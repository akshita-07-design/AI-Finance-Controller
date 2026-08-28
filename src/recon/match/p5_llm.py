"""
Pass 5 — LLM adjudication for the residue Passes 1-4 correctly decline to
resolve.

Central thesis of this whole engine: deterministic code decides, the LLM
only proposes, explains, and scores. Concretely, that means every guardrail
below is load-bearing, not decorative:

  1. The model NEVER emits an amount — the response schema has no field for
     one. It can only choose a candidate_id from a list WE supply.
  2. That candidate_id is validated against the offered list before being
     trusted at all — a hallucinated id (one we never offered) is rejected
     outright, never silently accepted.
  3. Every accepted proposal is independently re-verified arithmetically —
     the chosen settlement's amount must exactly equal the chosen bank row's
     net, checked by our own code, not taken on the model's word.
  4. Confidence below the threshold is treated as an exception regardless of
     what the model decided — "match" at confidence 0.3 is not a match.
  5. `escalate` is a first-class decision, not a last resort forced by
     schema constraints — a model that correctly says "I can't tell" should
     be believed, not pushed toward a guess.

One structural limit worth stating plainly: arithmetic re-verification
proves a proposed pairing is *consistent* — it cannot prove a pairing is the
unique *correct* one when several candidates are equally consistent (this
project's flagship ambiguous-duplicate case is exactly that: both candidate
settlements have the identical amount, so arithmetic re-verification passes
no matter which one the model picks). Verification catches "wrong answers
that don't add up." It cannot, by itself, catch "confident answers that add
up but happen to be the wrong one." That's precisely why confidence
calibration and the `escalate` option matter as much as the arithmetic
check — they're different, complementary defenses against different failure
modes.

A second limit, found only after building against the real API rather than
assuming: earlier versions of this module set `temperature=0` believing it
guaranteed identical output for an identical prompt. As of Gemini 3.6 Flash,
`temperature` (along with `top_p`/`top_k`) is deprecated and silently
IGNORED — no error, no warning, the call just succeeds while doing nothing.
True call-for-call determinism is no longer something client-side settings
can guarantee on this model generation. The prompt cache (llm_cache.py)
still earns its keep as a cost optimization — never re-paying for a
question already answered in this run — but it is no longer defensible to
claim it as a correctness guarantee. See FAILURE_LOG.md.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol
from pydantic import BaseModel, Field, ValidationError

from recon.models import BankRow, SettlementSummary

from .llm_cache import PromptCache, hash_prompt
from .types import AdjudicationDecision, AdjudicationRecord, AmbiguousGroup, P5Result

CONFIDENCE_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# The structured output schema — passed directly to Gemini as response_schema.
# This IS guardrail #1: there is no field here for an amount, only an id.
# ---------------------------------------------------------------------------

class AdjudicationResponse(BaseModel):
    decision: AdjudicationDecision
    candidate_id: str | None = Field(
        default=None,
        description="The bank_txn_id or settlement_id of the chosen candidate. "
                    "Null if decision is not 'match'.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=400)
    evidence: list[str] = Field(default_factory=list, max_length=6)


# ---------------------------------------------------------------------------
# LLM client — a Protocol, not a concrete class, so the guardrail logic below
# can be tested completely with a fake client. No network access, no API
# key, and no dependency on Gemini being reachable is needed to prove the
# guardrails work: see tests/test_p5_llm.py.
# ---------------------------------------------------------------------------

class LLMClient(Protocol):
    def generate(self, prompt: str) -> str: ...
    # `last_usage` is intentionally NOT part of the required Protocol
    # surface — it's read opportunistically via getattr() below, so a
    # minimal fake client in tests never needs to implement it.


class GeminiClient:
    """The real client, used in production. Reads GEMINI_API_KEY from the
    environment only — never accept a key as a literal argument, so it can
    never end up hardcoded in a script or a committed config file.

    Two things below were only discovered by running against the real API,
    not by reading the SDK's source in advance — see FAILURE_LOG.md:

    1. The free tier caps gemini-3.6-flash at 5 requests/minute. This client
       proactively paces calls to stay under that, and retries once with a
       generous backoff on a 429 rather than crashing the whole pipeline
       over one rate-limit hit.
    2. `response.usage_metadata` — the field that would normally carry real
       token counts — is populated for Vertex AI but NOT for the direct
       Gemini API this project uses (the SDK's own source literally says so
       in a docstring one line below the field names; it's easy to read
       past a warning like that when you're just looking for a field name
       to reference, which is exactly what happened here). Real API-metered
       token counts are simply not available through this access path, so
       cost is estimated from text length instead — clearly an estimate,
       never presented as an exact metered figure.
    """

    MIN_SECONDS_BETWEEN_CALLS = 13.0   # keeps us safely under 5 requests/60s
    RATE_LIMIT_BACKOFF_SECONDS = 65.0  # the API's own error suggested ~51s; padded for safety
    _CHARS_PER_TOKEN_ESTIMATE = 4      # a common rough English-text heuristic — not a metered count

    def __init__(self, model: str = "gemini-2.5-flash"):
        # Model choice matters here for a reason beyond capability: the
        # brand-new gemini-3.6-flash turned out to have a free-tier cap of
        # just 20 requests/DAY (confirmed by hitting it directly — Google's
        # own docs say "rate limits are more restricted for experimental
        # and preview models," and 3.6 Flash, released July 2026, is
        # squarely in that category). A daily quota doesn't clear with
        # retries or backoff — it only resets at midnight Pacific time, so
        # no amount of pacing fixes it within a session. gemini-2.5-flash
        # is an older, more established model with a meaningfully larger
        # free daily allowance, and the structured-output/schema behavior
        # this project depends on is the same across the Flash family. See
        # FAILURE_LOG.md. Check your OWN account's live numbers at
        # https://aistudio.google.com/rate-limit before assuming any
        # specific figure — entitlements can vary by account.
        from google import genai  # imported lazily so this module loads fine
                                    # even in environments without the SDK
                                    # installed, as long as GeminiClient is
                                    # never instantiated (e.g. CI running the
                                    # deterministic-only test suite).
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Set it as an environment variable "
                "— see README.md — never hardcode it in source."
            )
        self._client = genai.Client(api_key=api_key)
        self._model = os.environ.get("GEMINI_MODEL", model)
        self.last_usage: tuple[int | None, int | None] = (None, None)  # (prompt_tokens, output_tokens) — ESTIMATED, see class docstring
        self._last_call_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            remaining = self.MIN_SECONDS_BETWEEN_CALLS - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def generate(self, prompt: str) -> str:
        from google.genai import types
        from google.genai import errors as genai_errors

        self._wait_for_rate_limit()

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AdjudicationResponse,
                ),
            )
        except genai_errors.ClientError as e:
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                time.sleep(self.RATE_LIMIT_BACKOFF_SECONDS)
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=AdjudicationResponse,
                    ),
                )
            else:
                raise
        finally:
            self._last_call_at = time.monotonic()

        # See class docstring: usage_metadata is not populated on this API
        # access path, so this is a text-length estimate, not a real count.
        self.last_usage = (
            len(prompt) // self._CHARS_PER_TOKEN_ESTIMATE,
            len(response.text) // self._CHARS_PER_TOKEN_ESTIMATE,
        )
        return response.text


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _format_bank_row(row: BankRow) -> str:
    net = row.credit_paise - row.debit_paise
    return (
        f"  bank_txn_id: {row.bank_txn_id}\n"
        f"  date: {row.txn_date}  (value_date: {row.value_date})\n"
        f"  net amount: {net} paise\n"
        f"  narration: {row.narration!r}\n"
        f"  ref_no: {row.ref_no!r}\n"
    )


def _format_settlement_candidate(summary: SettlementSummary) -> str:
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(summary.created_at, tz=timezone.utc).date()
    return (
        f"  settlement_id: {summary.id}\n"
        f"  amount: {summary.amount} paise\n"
        f"  settle_date: {d}\n"
        f"  fees: {summary.fees} paise, tax: {summary.tax} paise\n"
        f"  utr (as recorded internally, NOT necessarily visible in the bank narration): {summary.utr}\n"
    )


def build_prompt(
    bank_row: BankRow,
    candidate_settlements: list[SettlementSummary],
    prior_pass_note: str,
) -> str:
    candidates_text = "\n".join(
        f"[{i}] {_format_settlement_candidate(s)}" for i, s in enumerate(candidate_settlements)
    )
    candidate_ids = ", ".join(f'"{s.id}"' for s in candidate_settlements)

    return f"""You are adjudicating ONE unresolved bank credit/debit row against a small
set of candidate settlement batches. Deterministic passes already tried
exact UTR matching, confusion-aware fuzzy UTR matching, and an amount+date
fallback — all inconclusive, which is why you're being asked.

UNRESOLVED BANK ROW
{_format_bank_row(bank_row)}

PRIOR PASSES
{prior_pass_note}

CANDIDATE SETTLEMENTS
{candidates_text}

INSTRUCTIONS
- If exactly one candidate is clearly the right match, return "match" with
  its settlement_id as candidate_id.
- If you are not genuinely confident — including if multiple candidates are
  equally plausible with no distinguishing signal — return "escalate".
  Escalating is the CORRECT answer when the evidence is truly ambiguous; do
  not guess to appear decisive, and do not invent a distinguishing detail
  that is not actually present in the data above.
- If you're confident NONE of the candidates match, return "no_match".
- candidate_id must be exactly one of: {candidate_ids} or null.
- Never state or rely on an amount other than what's shown above — you are
  not being asked to compute anything, only to identify which candidate (if
  any) this bank row corresponds to.

Return JSON matching the required schema only."""


# ---------------------------------------------------------------------------
# Guardrails + orchestration
# ---------------------------------------------------------------------------

def _verify_arithmetic(settlement: SettlementSummary, bank_row: BankRow) -> bool:
    """Independent re-check: does the proposed settlement's amount actually
    equal the proposed bank row's net? This is deterministic code re-doing
    the arithmetic itself — never trusting the model's own claim that a
    match is valid."""
    net = bank_row.credit_paise - bank_row.debit_paise
    return settlement.amount == net


def _call_and_verify(
    bank_row: BankRow,
    candidates: list[SettlementSummary],
    prior_pass_note: str,
    client: LLMClient,
    cache: PromptCache | None,
    confidence_threshold: float,
) -> AdjudicationRecord:
    prompt = build_prompt(bank_row, candidates, prior_pass_note)
    prompt_hash = hash_prompt(prompt)
    candidates_by_id = {s.id: s for s in candidates}

    from_cache = False
    cached = cache.get(prompt_hash) if cache else None
    start = time.monotonic()
    input_tokens: int | None = None
    output_tokens: int | None = None
    if cached is not None:
        raw_text = cached
        from_cache = True
    else:
        raw_text = client.generate(prompt)
        input_tokens, output_tokens = getattr(client, "last_usage", (None, None))
        if cache is not None:
            cache.set(prompt_hash, raw_text)
    latency_ms = (time.monotonic() - start) * 1000

    def rejected(reason: str, raw_decision=None, raw_candidate_id=None,
                 raw_confidence=None, reasoning="", evidence=None) -> AdjudicationRecord:
        return AdjudicationRecord(
            bank_txn_id=bank_row.bank_txn_id,
            candidate_settlement_ids=[s.id for s in candidates],
            raw_decision=raw_decision, raw_candidate_id=raw_candidate_id,
            raw_confidence=raw_confidence, reasoning=reasoning, evidence=evidence or [],
            accepted_match=None, rejection_reason=reason,
            prompt_hash=prompt_hash, latency_ms=latency_ms, from_cache=from_cache,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    # Guardrail: schema validation. A malformed or non-conforming response
    # is treated as an automatic escalation, never as a crash and never as
    # an accidental match.
    try:
        parsed = AdjudicationResponse.model_validate_json(raw_text)
    except (ValidationError, ValueError):
        return rejected("schema_invalid")

    if parsed.decision == AdjudicationDecision.ESCALATE:
        # Escalating is not a rejection — it's the model correctly declining
        # to guess. Recorded plainly, with no rejection_reason attached.
        return AdjudicationRecord(
            bank_txn_id=bank_row.bank_txn_id,
            candidate_settlement_ids=[s.id for s in candidates],
            raw_decision=parsed.decision, raw_candidate_id=parsed.candidate_id,
            raw_confidence=parsed.confidence, reasoning=parsed.reasoning, evidence=parsed.evidence,
            accepted_match=None, rejection_reason=None,
            prompt_hash=prompt_hash, latency_ms=latency_ms, from_cache=from_cache,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    if parsed.decision == AdjudicationDecision.NO_MATCH:
        return rejected(
            "model_said_no_match", raw_decision=parsed.decision,
            raw_candidate_id=parsed.candidate_id, raw_confidence=parsed.confidence,
            reasoning=parsed.reasoning, evidence=parsed.evidence,
        )

    # Guardrail: hallucination check. The model can ONLY choose from IDs we
    # actually offered — never trust a candidate_id we didn't supply.
    if parsed.candidate_id not in candidates_by_id:
        return rejected(
            "hallucinated_candidate_id", raw_decision=parsed.decision,
            raw_candidate_id=parsed.candidate_id, raw_confidence=parsed.confidence,
            reasoning=parsed.reasoning, evidence=parsed.evidence,
        )

    # Guardrail: confidence threshold, independent of the model's own
    # stated decision.
    if parsed.confidence < confidence_threshold:
        return rejected(
            "below_confidence_threshold", raw_decision=parsed.decision,
            raw_candidate_id=parsed.candidate_id, raw_confidence=parsed.confidence,
            reasoning=parsed.reasoning, evidence=parsed.evidence,
        )

    # Guardrail: independent arithmetic re-verification. This is the one
    # that matters most — a model proposing a match that doesn't actually
    # add up is rejected here regardless of its stated confidence.
    chosen_settlement = candidates_by_id[parsed.candidate_id]
    if not _verify_arithmetic(chosen_settlement, bank_row):
        return rejected(
            "arithmetic_mismatch", raw_decision=parsed.decision,
            raw_candidate_id=parsed.candidate_id, raw_confidence=parsed.confidence,
            reasoning=parsed.reasoning, evidence=parsed.evidence,
        )

    return AdjudicationRecord(
        bank_txn_id=bank_row.bank_txn_id,
        candidate_settlement_ids=[s.id for s in candidates],
        raw_decision=parsed.decision, raw_candidate_id=parsed.candidate_id,
        raw_confidence=parsed.confidence, reasoning=parsed.reasoning, evidence=parsed.evidence,
        accepted_match=(parsed.candidate_id, bank_row.bank_txn_id),
        rejection_reason=None,
        prompt_hash=prompt_hash, latency_ms=latency_ms, from_cache=from_cache,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def adjudicate(
    ambiguous_groups: list[AmbiguousGroup],
    summaries_by_id: dict[str, SettlementSummary],
    bank_by_id: dict[str, BankRow],
    client: LLMClient,
    cache: PromptCache | None = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> P5Result:
    """Process each ambiguous group one bank row at a time, maintaining a
    shrinking pool of not-yet-assigned settlement candidates within the
    group — once a settlement is accepted for one bank row, it's removed
    from the pool offered for the next, enforcing a 1:1 assignment."""
    matched: dict[str, str] = {}
    records: list[AdjudicationRecord] = []

    for group in ambiguous_groups:
        remaining_settlement_ids = list(group.settlement_ids)
        prior_pass_note = (
            f"Pass 1 (exact UTR) and Pass 4 (fuzzy UTR, then amount+date) both "
            f"failed to uniquely resolve this — reason recorded: {group.reason!r}."
        )

        for bank_txn_id in group.bank_txn_ids:
            if not remaining_settlement_ids:
                break  # more bank rows than settlements left in this group — shouldn't
                        # happen for a balanced collision, but never index past empty
            candidates = [summaries_by_id[sid] for sid in remaining_settlement_ids]
            record = _call_and_verify(
                bank_by_id[bank_txn_id], candidates, prior_pass_note,
                client, cache, confidence_threshold,
            )
            records.append(record)
            if record.accepted_match:
                sid, bid = record.accepted_match
                matched[sid] = bid
                remaining_settlement_ids.remove(sid)

    return P5Result(matched=matched, records=records)
