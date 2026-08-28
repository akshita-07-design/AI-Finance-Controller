"""
The evaluation scorecard.

Never imported by the matching engine — only by this module and its
callers. The whole point of held-out evaluation is that the matcher never
sees these labels; if that boundary gets blurred, every number downstream
is theatre.

Headline metric is FALSE MATCH RATE, not match rate. A wrong match silently
misstates the books; an unmatched record just waits for a human. See the
roadmap's Part 6 for the full reasoning — this module is where that
principle actually gets computed rather than just argued for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recon.match.engine import MatchReport
from recon.models import GroundTruth, SettlementSummary


@dataclass
class Scorecard:
    total_settlements: int
    total_records: int  # settlement lines, matching the engine's own denominator

    # --- bank-attribution correctness (the settlement <-> bank question) ---
    n_ground_truth_asserted: int       # settlements where ground truth asserts a definite bank_txn_id
    n_correct_matches: int
    n_false_matches: int               # WE predicted a bank_txn_id, ground truth says a DIFFERENT one
    n_declined_when_askable: int       # ground truth had an answer, we didn't attempt one
    false_match_settlement_ids: list[str] = field(default_factory=list)

    match_precision: float = 0.0       # correct / (correct + false) among settlements we DID predict for
    false_match_rate: float = 0.0      # false / (correct + false) — the headline number
    match_rate: float = 0.0            # correct / n_ground_truth_asserted
    value_weighted_match_rate: float = 0.0

    # --- exception quality (did we flag ambiguity where it actually exists?) ---
    n_flagged_ambiguous: int = 0
    n_flagged_ambiguous_correctly: int = 0   # ground truth actually expected this settlement to be an exception
    exception_precision: float = 0.0

    # --- LLM usage ---
    n_llm_calls: int = 0
    n_llm_accepted: int = 0
    n_llm_escalated: int = 0
    n_llm_rejected_by_guardrail: int = 0
    llm_invocation_rate: float = 0.0   # n_llm_calls / total_settlements
    llm_input_tokens: int = 0          # 0 for calls served entirely from cache
    llm_output_tokens: int = 0
    llm_estimated_cost_usd: float = 0.0


# Gemini 3.6 Flash introductory pricing (through 2026-12-31), per Google's
# own pricing page as of this project's build date — see FAILURE_LOG.md for
# the model-migration note this rate is tied to. Update if pricing changes.
_GEMINI_3_6_FLASH_INPUT_USD_PER_MILLION = 0.75
_GEMINI_3_6_FLASH_OUTPUT_USD_PER_MILLION = 3.75


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * _GEMINI_3_6_FLASH_INPUT_USD_PER_MILLION
        + output_tokens / 1_000_000 * _GEMINI_3_6_FLASH_OUTPUT_USD_PER_MILLION
    )


def compute_scorecard(
    report: MatchReport,
    ground_truth: GroundTruth,
    settlement_summaries: list[SettlementSummary],
) -> Scorecard:
    summaries_by_id = {s.id: s for s in settlement_summaries}

    # Ground truth's `matches` list has one entry per settlement LINE, but
    # bank-attribution is a settlement-level question — collapse to one
    # asserted bank_txn_id per distinct settlement_id (they all agree by
    # construction, since every line in a batch shares the same pairing).
    gt_bank_txn_by_settlement: dict[str, str | None] = {}
    for m in ground_truth.matches:
        if m.settlement_id is not None:
            gt_bank_txn_by_settlement[m.settlement_id] = m.bank_txn_id

    # settlement_ids ground truth expects to be genuinely unresolvable
    # (the ambiguous-duplicate-amount case) — pulled from expected_exceptions'
    # record_id format "settlement_id|bank_txn_id".
    expected_exception_settlement_ids = {
        exc.record_id.split("|")[0] for exc in ground_truth.expected_exceptions
        if exc.reason_code == "AMBIGUOUS_DUPLICATE_AMOUNT"
    }

    n_correct = 0
    n_false = 0
    n_declined_when_askable = 0
    false_ids: list[str] = []
    correct_value = 0
    total_askable_value = 0

    all_settlement_ids = set(gt_bank_txn_by_settlement.keys())
    n_ground_truth_asserted = sum(
        1 for sid, bid in gt_bank_txn_by_settlement.items() if bid is not None
    )

    for sid, gt_bank_txn_id in gt_bank_txn_by_settlement.items():
        if gt_bank_txn_id is None:
            continue  # ground truth itself doesn't assert a definite answer here — not scoreable
        our_prediction = report.settlement_to_bank_txn.get(sid)
        amount = summaries_by_id[sid].amount if sid in summaries_by_id else 0
        total_askable_value += amount

        if our_prediction is None:
            n_declined_when_askable += 1
        elif our_prediction == gt_bank_txn_id:
            n_correct += 1
            correct_value += amount
        else:
            n_false += 1
            false_ids.append(sid)

    attempted = n_correct + n_false
    match_precision = n_correct / attempted if attempted else 1.0
    false_match_rate = n_false / attempted if attempted else 0.0
    match_rate = n_correct / n_ground_truth_asserted if n_ground_truth_asserted else 1.0
    value_weighted = correct_value / total_askable_value if total_askable_value else 1.0

    n_flagged_ambiguous = len({
        sid for group in report.ambiguous_groups for sid in group.settlement_ids
    })
    n_flagged_correctly = len({
        sid for group in report.ambiguous_groups for sid in group.settlement_ids
        if sid in expected_exception_settlement_ids
    })
    exception_precision = (
        n_flagged_correctly / n_flagged_ambiguous if n_flagged_ambiguous else 1.0
    )

    n_llm_calls = len(report.p5.records) if report.p5 else 0
    n_llm_accepted = len(report.p5.matched) if report.p5 else 0
    n_llm_escalated = sum(
        1 for r in report.p5.records if r.rejection_reason is None and r.accepted_match is None
    ) if report.p5 else 0
    n_llm_rejected = sum(
        1 for r in report.p5.records if r.rejection_reason is not None
    ) if report.p5 else 0
    llm_input_tokens = sum(
        r.input_tokens or 0 for r in report.p5.records
    ) if report.p5 else 0
    llm_output_tokens = sum(
        r.output_tokens or 0 for r in report.p5.records
    ) if report.p5 else 0

    return Scorecard(
        total_settlements=len(settlement_summaries),
        total_records=len(report.classifications),
        n_ground_truth_asserted=n_ground_truth_asserted,
        n_correct_matches=n_correct,
        n_false_matches=n_false,
        n_declined_when_askable=n_declined_when_askable,
        false_match_settlement_ids=false_ids,
        match_precision=match_precision,
        false_match_rate=false_match_rate,
        match_rate=match_rate,
        value_weighted_match_rate=value_weighted,
        n_flagged_ambiguous=n_flagged_ambiguous,
        n_flagged_ambiguous_correctly=n_flagged_correctly,
        exception_precision=exception_precision,
        n_llm_calls=n_llm_calls,
        n_llm_accepted=n_llm_accepted,
        n_llm_escalated=n_llm_escalated,
        n_llm_rejected_by_guardrail=n_llm_rejected,
        llm_invocation_rate=(n_llm_calls / len(settlement_summaries)) if settlement_summaries else 0.0,
        llm_input_tokens=llm_input_tokens,
        llm_output_tokens=llm_output_tokens,
        llm_estimated_cost_usd=_estimate_cost_usd(llm_input_tokens, llm_output_tokens),
    )


def format_scorecard(sc: Scorecard) -> str:
    lines = [
        "=" * 60,
        " RECONCILIATION SCORECARD",
        "=" * 60,
        f" Settlements                    {sc.total_settlements}",
        f" Settlement lines (records)     {sc.total_records}",
        "",
        " BANK-ATTRIBUTION CORRECTNESS",
        f"   Ground truth askable          {sc.n_ground_truth_asserted}",
        f"   Correct matches               {sc.n_correct_matches}",
        f"   False matches                 {sc.n_false_matches}  <- headline risk metric",
        f"   Declined (no answer given)    {sc.n_declined_when_askable}",
        f"   Match rate                    {sc.match_rate:.1%}",
        f"   Match precision               {sc.match_precision:.1%}",
        f"   False match rate              {sc.false_match_rate:.1%}  <- headline metric",
        f"   Value-weighted match rate     {sc.value_weighted_match_rate:.1%}",
        "",
        " EXCEPTION QUALITY",
        f"   Flagged ambiguous             {sc.n_flagged_ambiguous}",
        f"   ...of which correctly so      {sc.n_flagged_ambiguous_correctly}",
        f"   Exception precision           {sc.exception_precision:.1%}",
        "",
        " LLM LAYER",
        f"   Calls made                    {sc.n_llm_calls}",
        f"   Accepted                      {sc.n_llm_accepted}",
        f"   Escalated (correctly declined){sc.n_llm_escalated}",
        f"   Rejected by guardrail         {sc.n_llm_rejected_by_guardrail}",
        f"   Invocation rate               {sc.llm_invocation_rate:.1%}",
        f"   Tokens (in/out)               {sc.llm_input_tokens:,} / {sc.llm_output_tokens:,}",
        f"   Estimated cost (this run)     ${sc.llm_estimated_cost_usd:.4f}",
        (
            f"   Estimated cost / 1,000 records "
            f"${(sc.llm_estimated_cost_usd / sc.total_records * 1000):.4f}"
            if sc.total_records else "   Estimated cost / 1,000 records  n/a"
        ),
        "=" * 60,
    ]
    if sc.false_match_settlement_ids:
        lines.append(f" False match settlement ids: {sc.false_match_settlement_ids}")
    return "\n".join(lines)
