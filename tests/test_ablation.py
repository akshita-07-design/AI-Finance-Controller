"""
Tests for the ablation baseline, using a fake LLM client — same pattern as
test_p5_llm.py. The real, quota-consuming run against Gemini is a deliberate
manual step (scripts/run_ablation.py), not part of the automated suite.
"""

from __future__ import annotations

import json
from datetime import date

from recon.evaluate.ablation import (
    run_ablation,
    sample_scoreable_bank_rows,
)
from recon.models import (
    BankRow,
    ExpectedException,
    GeneratorConfig,
    GroundTruth,
    GroundTruthMatch,
    MatchType,
    SettlementSummary,
)


class FakeLLMClient:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)

    def generate(self, prompt: str) -> str:
        return self._responses.pop(0)


def make_bank_row(bank_txn_id, credit=0, debit=0):
    return BankRow(
        bank_txn_id=bank_txn_id, txn_date=date(2026, 4, 3), value_date=date(2026, 4, 3),
        narration="test", ref_no=None, debit_paise=debit, credit_paise=credit,
        balance_paise=1_000_000,
    )


def make_summary(id_, amount):
    return SettlementSummary(id=id_, amount=amount, fees=0, tax=0, utr=f"utr_{id_}", created_at=1_775_000_000)


def make_ground_truth(matches, expected_exceptions=None):
    return GroundTruth(
        generator_config=GeneratorConfig(seed=42, month="2026-04", n_orders=1, anomaly_rates={}),
        matches=matches, expected_exceptions=expected_exceptions or [],
    )


def match_json(candidate_id, confidence=0.9):
    return json.dumps({"decision": "match", "candidate_id": candidate_id,
                        "confidence": confidence, "reasoning": "x", "evidence": []})


def no_match_json(confidence=0.9):
    return json.dumps({"decision": "no_match", "candidate_id": None,
                        "confidence": confidence, "reasoning": "x", "evidence": []})


def escalate_json(confidence=0.4):
    return json.dumps({"decision": "escalate", "candidate_id": None,
                        "confidence": confidence, "reasoning": "x", "evidence": []})


class TestSampling:
    def test_ambiguous_pair_bank_rows_are_excluded_from_the_sample_pool(self):
        rows = [make_bank_row("BNK-1"), make_bank_row("BNK-2"), make_bank_row("BNK-3")]
        gt = make_ground_truth([], expected_exceptions=[
            ExpectedException(record_id="setl_A|BNK-1", reason_code="AMBIGUOUS_DUPLICATE_AMOUNT"),
        ])
        sample, truth_map = sample_scoreable_bank_rows(rows, gt, n=10)
        assert "BNK-1" not in [r.bank_txn_id for r in sample]
        assert len(sample) == 2

    def test_truth_map_correctly_derived_from_ground_truth_matches(self):
        rows = [make_bank_row("BNK-1")]
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_A", bank_txn_id="BNK-1",
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
        ])
        sample, truth_map = sample_scoreable_bank_rows(rows, gt, n=10)
        assert truth_map["BNK-1"] == "setl_A"

    def test_row_with_no_ground_truth_entry_maps_to_none(self):
        """A noise row (never appears in ground truth matches) should map
        to None — meaning 'correct answer is no_match'."""
        rows = [make_bank_row("BNK-NOISE")]
        gt = make_ground_truth([])
        sample, truth_map = sample_scoreable_bank_rows(rows, gt, n=10)
        assert truth_map["BNK-NOISE"] is None


class TestScoring:
    def test_correct_match_is_scored_correct(self):
        settlements = [make_summary("setl_A", 50_000)]
        client = FakeLLMClient([match_json("setl_A")])
        result = run_ablation(
            [make_bank_row("BNK-1", credit=50_000)], {"BNK-1": "setl_A"}, settlements, client,
        )
        assert result.n_correct == 1
        assert result.n_false_matches == 0

    def test_wrong_candidate_is_a_false_match(self):
        settlements = [make_summary("setl_A", 50_000), make_summary("setl_B", 50_000)]
        client = FakeLLMClient([match_json("setl_B")])  # true answer is setl_A
        result = run_ablation(
            [make_bank_row("BNK-1", credit=50_000)], {"BNK-1": "setl_A"}, settlements, client,
        )
        assert result.n_false_matches == 1
        assert result.false_match_rate == 1.0

    def test_matching_something_to_a_noise_row_is_a_false_match(self):
        """The model confidently matches a bank row that should have been
        'no_match' — this is exactly the false-positive risk the ablation
        is designed to surface."""
        settlements = [make_summary("setl_A", 50_000)]
        client = FakeLLMClient([match_json("setl_A", confidence=0.95)])
        result = run_ablation(
            [make_bank_row("BNK-NOISE", credit=999)], {"BNK-NOISE": None}, settlements, client,
        )
        assert result.n_false_matches == 1

    def test_correctly_saying_no_match_on_a_noise_row_is_correct(self):
        settlements = [make_summary("setl_A", 50_000)]
        client = FakeLLMClient([no_match_json()])
        result = run_ablation(
            [make_bank_row("BNK-NOISE", credit=999)], {"BNK-NOISE": None}, settlements, client,
        )
        assert result.n_correct == 1
        assert result.n_false_matches == 0

    def test_escalate_is_tracked_separately_not_counted_as_false_match(self):
        settlements = [make_summary("setl_A", 50_000)]
        client = FakeLLMClient([escalate_json()])
        result = run_ablation(
            [make_bank_row("BNK-1", credit=50_000)], {"BNK-1": "setl_A"}, settlements, client,
        )
        assert result.n_escalated == 1
        assert result.n_false_matches == 0
        assert result.false_match_rate == 0.0  # not counted in the attempted denominator

    def test_schema_invalid_response_does_not_crash(self):
        settlements = [make_summary("setl_A", 50_000)]
        client = FakeLLMClient(["not valid json {"])
        result = run_ablation(
            [make_bank_row("BNK-1", credit=50_000)], {"BNK-1": "setl_A"}, settlements, client,
        )
        assert result.n_schema_invalid == 1
