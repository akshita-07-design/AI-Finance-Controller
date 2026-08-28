"""
Tests for Pass 5 (LLM adjudication), using a FakeLLMClient that returns
canned JSON strings.

This is deliberate: the guardrail logic (schema validation, hallucination
rejection, confidence thresholding, arithmetic re-verification) is the part
that actually matters, and none of it depends on a real model being
reachable. Testing it against a fake client that can return any string we
want — including deliberately broken ones — gives FULL coverage of every
guardrail without needing network access or an API key. The real
GeminiClient is a thin wrapper verified separately, by hand, against the
actual API (see scripts/test_llm_connection.py).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from recon.match.p5_llm import CONFIDENCE_THRESHOLD, adjudicate
from recon.match.types import AmbiguousGroup
from recon.models import BankRow, SettlementSummary


class FakeLLMClient:
    """Returns pre-scripted responses in call order — one string per call,
    regardless of prompt content. Good enough for testing guardrail
    behavior; not a semantic simulation of a real model."""
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts_seen: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts_seen.append(prompt)
        return self._responses.pop(0)


def make_summary(id_, amount, utr="utrX", created_at=1_775_000_000, fees=0, tax=0):
    return SettlementSummary(id=id_, amount=amount, fees=fees, tax=tax, utr=utr, created_at=created_at)


def make_bank_row(bank_txn_id, credit=0, debit=0):
    return BankRow(
        bank_txn_id=bank_txn_id, txn_date=date(2026, 4, 3), value_date=date(2026, 4, 3),
        narration="NEFT CR: HDFC SETTLEMENT", ref_no=None,
        debit_paise=debit, credit_paise=credit, balance_paise=1_000_000,
    )


def valid_match_json(candidate_id, confidence=0.95):
    return json.dumps({
        "decision": "match", "candidate_id": candidate_id, "confidence": confidence,
        "reasoning": "test reasoning", "evidence": ["test evidence"],
    })


def valid_escalate_json(confidence=0.4):
    return json.dumps({
        "decision": "escalate", "candidate_id": None, "confidence": confidence,
        "reasoning": "genuinely ambiguous", "evidence": [],
    })


class TestHappyPath:
    def test_confident_correct_match_is_accepted(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([valid_match_json("setl_A", confidence=0.95)])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {"setl_A": "BNK-1"}
        assert result.records[0].accepted_match == ("setl_A", "BNK-1")
        assert result.records[0].rejection_reason is None

    def test_escalate_is_recorded_as_correct_behavior_not_a_rejection(self):
        settlements = {
            "setl_A": make_summary("setl_A", 50_000),
            "setl_B": make_summary("setl_B", 50_000),
        }
        bank_rows = {
            "BNK-1": make_bank_row("BNK-1", credit=50_000),
            "BNK-2": make_bank_row("BNK-2", credit=50_000),
        }
        group = AmbiguousGroup(
            settlement_ids=["setl_A", "setl_B"], bank_txn_ids=["BNK-1", "BNK-2"], reason="test",
        )
        client = FakeLLMClient([valid_escalate_json(), valid_escalate_json()])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {}
        for record in result.records:
            assert record.raw_decision.value == "escalate"
            assert record.rejection_reason is None  # escalating correctly is not a rejection
            assert record.accepted_match is None


class TestGuardrails:
    def test_schema_invalid_response_is_rejected_not_crashed(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient(["this is not json at all { broken"])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {}
        assert result.records[0].rejection_reason == "schema_invalid"

    def test_missing_required_field_is_rejected(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        # missing "confidence" entirely
        client = FakeLLMClient([json.dumps({
            "decision": "match", "candidate_id": "setl_A", "reasoning": "x", "evidence": [],
        })])

        result = adjudicate([group], settlements, bank_rows, client)
        assert result.records[0].rejection_reason == "schema_invalid"

    def test_hallucinated_candidate_id_is_rejected(self):
        """The model returns an id we never offered as a candidate — must
        be rejected outright, never silently accepted as if it were valid."""
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([valid_match_json("setl_ZZZ_NEVER_OFFERED", confidence=0.99)])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {}
        assert result.records[0].rejection_reason == "hallucinated_candidate_id"

    def test_low_confidence_match_is_rejected_even_if_decision_is_match(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([valid_match_json("setl_A", confidence=0.5)])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {}
        assert result.records[0].rejection_reason == "below_confidence_threshold"

    def test_confidence_exactly_at_threshold_is_accepted(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([valid_match_json("setl_A", confidence=CONFIDENCE_THRESHOLD)])

        result = adjudicate([group], settlements, bank_rows, client)
        assert result.matched == {"setl_A": "BNK-1"}

    def test_arithmetic_mismatch_is_rejected_even_at_high_confidence(self):
        """The single most important guardrail: a model that confidently
        proposes a match which doesn't actually add up must be rejected by
        our own independent check, regardless of stated confidence."""
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=99_999)}  # doesn't match 50_000
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([valid_match_json("setl_A", confidence=0.99)])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {}
        assert result.records[0].rejection_reason == "arithmetic_mismatch"

    def test_model_saying_no_match_is_recorded_as_such(self):
        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        client = FakeLLMClient([json.dumps({
            "decision": "no_match", "candidate_id": None, "confidence": 0.9,
            "reasoning": "x", "evidence": [],
        })])

        result = adjudicate([group], settlements, bank_rows, client)
        assert result.matched == {}
        assert result.records[0].rejection_reason == "model_said_no_match"


class TestGroupAssignment:
    def test_accepted_settlement_is_removed_from_pool_for_next_bank_row(self):
        """Once setl_A is accepted for BNK-1, it must not be offered again
        as a candidate for BNK-2 in the same group — enforces a 1:1
        assignment within the group."""
        settlements = {
            "setl_A": make_summary("setl_A", 50_000),
            "setl_B": make_summary("setl_B", 50_000),
        }
        bank_rows = {
            "BNK-1": make_bank_row("BNK-1", credit=50_000),
            "BNK-2": make_bank_row("BNK-2", credit=50_000),
        }
        group = AmbiguousGroup(
            settlement_ids=["setl_A", "setl_B"], bank_txn_ids=["BNK-1", "BNK-2"], reason="test",
        )
        client = FakeLLMClient([
            valid_match_json("setl_A", confidence=0.95),  # BNK-1 -> setl_A
            valid_match_json("setl_B", confidence=0.95),  # BNK-2 -> setl_B (only one left)
        ])

        result = adjudicate([group], settlements, bank_rows, client)

        assert result.matched == {"setl_A": "BNK-1", "setl_B": "BNK-2"}
        # The second prompt should only have offered setl_B as a candidate
        assert "setl_A" not in client.prompts_seen[1] or "CANDIDATE SETTLEMENTS" in client.prompts_seen[1]
        second_prompt_candidates_section = client.prompts_seen[1].split("CANDIDATE SETTLEMENTS")[1]
        assert "setl_A" not in second_prompt_candidates_section
        assert "setl_B" in second_prompt_candidates_section


class TestCaching:
    def test_identical_prompt_is_served_from_cache_not_called_again(self, tmp_path):
        from recon.match.llm_cache import PromptCache

        settlements = {"setl_A": make_summary("setl_A", 50_000)}
        bank_rows = {"BNK-1": make_bank_row("BNK-1", credit=50_000)}
        group = AmbiguousGroup(settlement_ids=["setl_A"], bank_txn_ids=["BNK-1"], reason="test")
        cache = PromptCache(tmp_path / "cache.json")

        client_1 = FakeLLMClient([valid_match_json("setl_A", confidence=0.95)])
        result_1 = adjudicate([group], settlements, bank_rows, client_1, cache=cache)
        assert result_1.records[0].from_cache is False

        # A second client with NO responses queued — if the cache didn't
        # work, this would raise IndexError on an empty list.
        client_2 = FakeLLMClient([])
        cache_2 = PromptCache(tmp_path / "cache.json")  # reload from disk
        result_2 = adjudicate([group], settlements, bank_rows, client_2, cache=cache_2)
        assert result_2.records[0].from_cache is True
        assert result_2.matched == {"setl_A": "BNK-1"}
