"""
Ablation: the "LLM on everything" baseline.

Bypasses Passes 1-4 entirely. For a SAMPLE of bank rows, the model gets the
row plus the FULL list of settlement candidates — no deterministic
pre-filtering, no prior-pass narrowing, no arithmetic proof. This is the
argument for the 5-pass design made as evidence rather than assertion: if
this baseline produces more false matches, at a higher cost, than the real
pipeline's measured false-match rate, that comparison IS the ablation
table's point — see roadmap Part 6.4.

Uses the same AdjudicationResponse schema and LLMClient protocol as
p5_llm.py, so this module's scoring logic is fully testable with a fake
client, exactly like Pass 5's own tests — the real, quota-consuming run
against Gemini is something to run deliberately and sparingly, not on every
test suite invocation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from recon.match.llm_cache import PromptCache, hash_prompt
from recon.match.p5_llm import AdjudicationResponse, LLMClient
from recon.match.types import AdjudicationDecision
from recon.models import BankRow, GroundTruth, SettlementSummary


@dataclass
class AblationRecord:
    bank_txn_id: str
    raw_decision: AdjudicationDecision | None
    raw_candidate_id: str | None
    raw_confidence: float | None
    true_settlement_id: str | None   # None means "should be no_match" (noise/reversal/etc.)
    outcome: str                      # "correct" | "false_match" | "escalated" | "schema_invalid"
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class AblationResult:
    records: list[AblationRecord] = field(default_factory=list)
    n_sampled: int = 0
    n_correct: int = 0
    n_false_matches: int = 0
    n_escalated: int = 0
    n_schema_invalid: int = 0
    false_match_rate: float = 0.0   # of ATTEMPTED (non-escalated) decisions
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


def _format_settlement_candidate(summary: SettlementSummary) -> str:
    d = datetime.fromtimestamp(summary.created_at, tz=timezone.utc).date()
    return (
        f"  settlement_id: {summary.id}, amount: {summary.amount} paise, "
        f"settle_date: {d}, fees: {summary.fees}, tax: {summary.tax}"
    )


def build_ablation_prompt(bank_row: BankRow, all_settlements: list[SettlementSummary]) -> str:
    """Deliberately gives NO prior-pass context and NO narrowed candidate
    list — the entire point is testing what the model does with zero
    deterministic help, as a baseline against the real 5-pass pipeline."""
    net = bank_row.credit_paise - bank_row.debit_paise
    candidates_text = "\n".join(_format_settlement_candidate(s) for s in all_settlements)
    candidate_ids = ", ".join(f'"{s.id}"' for s in all_settlements)

    return f"""You are reconciling ONE bank statement row against a merchant's full list
of settlement batches for the month. No prior filtering has been done —
you have the complete candidate list.

BANK ROW
  bank_txn_id: {bank_row.bank_txn_id}
  date: {bank_row.txn_date}
  net amount: {net} paise
  narration: {bank_row.narration!r}
  ref_no: {bank_row.ref_no!r}

ALL SETTLEMENT BATCHES THIS MONTH
{candidates_text}

INSTRUCTIONS
- If exactly one candidate is clearly the right match, return "match" with
  its settlement_id as candidate_id.
- If you're confident this bank row does NOT correspond to any candidate
  here (e.g. it looks like an unrelated transaction — salary, a vendor
  payment, a bank fee), return "no_match".
- If you are not genuinely confident either way, return "escalate".
- candidate_id must be exactly one of: {candidate_ids} or null.

Return JSON matching the required schema only."""


def sample_scoreable_bank_rows(
    bank_rows: list[BankRow],
    ground_truth: GroundTruth,
    n: int,
    seed: int = 0,
) -> tuple[list[BankRow], dict[str, str | None]]:
    """Pick a random sample of bank rows to evaluate, EXCLUDING rows that
    belong to a genuinely ambiguous pair (seeded #12) — those have no single
    correct answer in ground truth (deliberately, see ground_truth.py), so
    scoring the ablation baseline against them would be unfair to whichever
    of the two candidates it happens not to pick, in a way that has nothing
    to do with the baseline's actual quality.

    Returns the sample plus a bank_txn_id -> true_settlement_id map (None
    for rows that should correctly be "no_match" — noise, reversals, or
    anything else without a definite settlement behind it).
    """
    ambiguous_bank_ids = {
        exc.record_id.split("|")[1] for exc in ground_truth.expected_exceptions
        if exc.reason_code == "AMBIGUOUS_DUPLICATE_AMOUNT" and "|" in exc.record_id
        and exc.record_id.split("|")[1] not in ("None", "")
    }

    bank_txn_to_settlement: dict[str, str | None] = {}
    for m in ground_truth.matches:
        if m.bank_txn_id is not None:
            bank_txn_to_settlement[m.bank_txn_id] = m.settlement_id

    eligible = [r for r in bank_rows if r.bank_txn_id not in ambiguous_bank_ids]
    rng = random.Random(seed)
    sample = rng.sample(eligible, k=min(n, len(eligible)))

    truth_map = {r.bank_txn_id: bank_txn_to_settlement.get(r.bank_txn_id) for r in sample}
    return sample, truth_map


def run_ablation(
    sample: list[BankRow],
    truth_map: dict[str, str | None],
    all_settlements: list[SettlementSummary],
    client: LLMClient,
    cache: PromptCache | None = None,
) -> AblationResult:
    records: list[AblationRecord] = []

    for row in sample:
        prompt = build_ablation_prompt(row, all_settlements)
        prompt_hash = hash_prompt(prompt)
        cached = cache.get(prompt_hash) if cache else None
        input_tokens: int | None = None
        output_tokens: int | None = None
        if cached is not None:
            raw_text = cached
        else:
            raw_text = client.generate(prompt)
            input_tokens, output_tokens = getattr(client, "last_usage", (None, None))
            if cache is not None:
                cache.set(prompt_hash, raw_text)

        true_settlement_id = truth_map.get(row.bank_txn_id)

        try:
            parsed = AdjudicationResponse.model_validate_json(raw_text)
        except (ValidationError, ValueError):
            records.append(AblationRecord(
                bank_txn_id=row.bank_txn_id, raw_decision=None, raw_candidate_id=None,
                raw_confidence=None, true_settlement_id=true_settlement_id,
                outcome="schema_invalid", input_tokens=input_tokens, output_tokens=output_tokens,
            ))
            continue

        if parsed.decision == AdjudicationDecision.ESCALATE:
            outcome = "escalated"
        elif parsed.decision == AdjudicationDecision.NO_MATCH:
            outcome = "correct" if true_settlement_id is None else "false_match"
            # a NO_MATCH decision when a real settlement existed is a missed
            # match, not a false POSITIVE — but for this ablation's purpose
            # (measuring cost of skipping deterministic passes) a missed
            # match on a record the real pipeline resolves cleanly is still
            # a meaningful quality gap, so it's tracked under false_match
            # here for a conservative (harsher) baseline comparison.
        else:  # MATCH
            if true_settlement_id is not None and parsed.candidate_id == true_settlement_id:
                outcome = "correct"
            else:
                outcome = "false_match"

        records.append(AblationRecord(
            bank_txn_id=row.bank_txn_id, raw_decision=parsed.decision,
            raw_candidate_id=parsed.candidate_id, raw_confidence=parsed.confidence,
            true_settlement_id=true_settlement_id, outcome=outcome,
            input_tokens=input_tokens, output_tokens=output_tokens,
        ))

    n_correct = sum(1 for r in records if r.outcome == "correct")
    n_false = sum(1 for r in records if r.outcome == "false_match")
    n_escalated = sum(1 for r in records if r.outcome == "escalated")
    n_invalid = sum(1 for r in records if r.outcome == "schema_invalid")
    attempted = n_correct + n_false
    total_input_tokens = sum(r.input_tokens or 0 for r in records)
    total_output_tokens = sum(r.output_tokens or 0 for r in records)

    return AblationResult(
        records=records, n_sampled=len(sample), n_correct=n_correct,
        n_false_matches=n_false, n_escalated=n_escalated, n_schema_invalid=n_invalid,
        false_match_rate=(n_false / attempted) if attempted else 0.0,
        input_tokens=total_input_tokens, output_tokens=total_output_tokens,
        estimated_cost_usd=(
            total_input_tokens / 1_000_000 * 0.75 + total_output_tokens / 1_000_000 * 3.75
        ),
    )


def format_ablation_result(result: AblationResult) -> str:
    lines = [
        "=" * 60,
        " ABLATION: 'LLM on everything' baseline",
        "=" * 60,
        f" Sampled                {result.n_sampled}",
        f" Correct                {result.n_correct}",
        f" False matches          {result.n_false_matches}  <- compare to the real pipeline's false matches",
        f" Escalated              {result.n_escalated}",
        f" Schema invalid         {result.n_schema_invalid}",
        f" False match rate       {result.false_match_rate:.1%}  <- compare to the real pipeline's false match rate",
        f" Tokens (in/out)        {result.input_tokens:,} / {result.output_tokens:,}",
        f" Estimated cost         ${result.estimated_cost_usd:.4f}  for {result.n_sampled} records",
        (
            f" Estimated cost/1,000   ${(result.estimated_cost_usd / result.n_sampled * 1000):.4f}"
            if result.n_sampled else " Estimated cost/1,000   n/a"
        ),
        "=" * 60,
    ]
    return "\n".join(lines)
