"""
Tests for the scorecard, using hand-built MatchReport and GroundTruth
fixtures rather than the full generator+engine pipeline — each test isolates
one specific piece of the arithmetic (a correct match, a false match, a
correctly-declined case, value weighting, exception precision, LLM stats).
"""

from __future__ import annotations

from recon.evaluate.metrics import compute_scorecard
from recon.match.engine import MatchReport
from recon.match.types import (
    AdjudicationDecision,
    AdjudicationRecord,
    AmbiguousGroup,
    P1Result,
    P2Result,
    P3Result,
    P4Result,
    P5Result,
)
from recon.models import (
    ExpectedException,
    GeneratorConfig,
    GroundTruth,
    GroundTruthMatch,
    MatchType,
)


def make_settlement_summary(id_, amount):
    from recon.models import SettlementSummary
    return SettlementSummary(id=id_, amount=amount, fees=0, tax=0, utr=f"utr_{id_}", created_at=1_775_000_000)


def make_ground_truth(matches, expected_exceptions=None):
    return GroundTruth(
        generator_config=GeneratorConfig(seed=42, month="2026-04", n_orders=1, anomaly_rates={}),
        matches=matches,
        expected_exceptions=expected_exceptions or [],
    )


def make_report(settlement_to_bank_txn=None, ambiguous_groups=None, p5=None, n_records=1):
    return MatchReport(
        p1=P1Result(), p4=P4Result(), p2=P2Result(), p3=P3Result(),
        classifications={f"rec_{i}": None for i in range(n_records)},  # only count matters for total_records
        ambiguous_groups=ambiguous_groups or [],
        p5=p5,
        settlement_to_bank_txn=settlement_to_bank_txn or {},
    )


class TestBankAttributionCorrectness:
    def test_correct_match_scores_perfectly(self):
        report = make_report(settlement_to_bank_txn={"setl_A": "BNK-1"})
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_A", bank_txn_id="BNK-1",
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
        ])
        sc = compute_scorecard(report, gt, [make_settlement_summary("setl_A", 50_000)])

        assert sc.n_correct_matches == 1
        assert sc.n_false_matches == 0
        assert sc.false_match_rate == 0.0
        assert sc.match_rate == 1.0

    def test_false_match_is_detected_and_named(self):
        report = make_report(settlement_to_bank_txn={"setl_A": "BNK-WRONG"})
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_A", bank_txn_id="BNK-1",
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
        ])
        sc = compute_scorecard(report, gt, [make_settlement_summary("setl_A", 50_000)])

        assert sc.n_false_matches == 1
        assert sc.false_match_rate == 1.0
        assert "setl_A" in sc.false_match_settlement_ids

    def test_declining_to_predict_is_not_scored_as_a_false_match(self):
        """Declining is not the same as being wrong — the false match rate
        must only be computed over settlements we actually took a position
        on."""
        report = make_report(settlement_to_bank_txn={})  # no prediction at all
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_A", bank_txn_id="BNK-1",
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
        ])
        sc = compute_scorecard(report, gt, [make_settlement_summary("setl_A", 50_000)])

        assert sc.n_declined_when_askable == 1
        assert sc.n_false_matches == 0
        assert sc.false_match_rate == 0.0   # attempted=0, not scored as failure
        assert sc.match_rate == 0.0          # but match_rate correctly reflects we got nothing

    def test_ground_truth_null_bank_txn_id_is_excluded_from_askable_denominator(self):
        """A settlement in an ambiguous pair has bank_txn_id=None in ground
        truth (see ground_truth.py) — it must not be counted as something we
        'should have' matched, since the generator itself can't assert a
        single correct answer there."""
        report = make_report(settlement_to_bank_txn={})
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_AMBIG", bank_txn_id=None,
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
        ])
        sc = compute_scorecard(report, gt, [make_settlement_summary("setl_AMBIG", 50_000)])

        assert sc.n_ground_truth_asserted == 0
        assert sc.n_declined_when_askable == 0

    def test_value_weighted_match_rate_weights_by_settlement_amount(self):
        """A cheap correct match and an expensive false match should NOT
        average to 50% under value weighting — the expensive one dominates."""
        report = make_report(settlement_to_bank_txn={"setl_small": "BNK-1", "setl_big": "BNK-WRONG"})
        gt = make_ground_truth([
            GroundTruthMatch(match_id="m1", settlement_id="setl_small", bank_txn_id="BNK-1",
                              settlement_entity_id="pay_1", match_type=MatchType.THREE_WAY_EXACT),
            GroundTruthMatch(match_id="m2", settlement_id="setl_big", bank_txn_id="BNK-2",
                              settlement_entity_id="pay_2", match_type=MatchType.THREE_WAY_EXACT),
        ])
        summaries = [
            make_settlement_summary("setl_small", 1_000),      # ₹10
            make_settlement_summary("setl_big", 10_000_000),   # ₹1,00,000
        ]
        sc = compute_scorecard(report, gt, summaries)

        # count-weighted match rate is 50% (1 of 2 correct)...
        assert sc.match_rate == 0.5
        # ...but value-weighted match rate should be tiny, since the huge
        # settlement is the one that's wrong
        assert sc.value_weighted_match_rate < 0.01


class TestExceptionQuality:
    def test_correctly_flagged_ambiguity_scores_full_precision(self):
        report = make_report(
            ambiguous_groups=[AmbiguousGroup(settlement_ids=["setl_A", "setl_B"],
                                              bank_txn_ids=["BNK-1", "BNK-2"], reason="test")],
        )
        gt = make_ground_truth([], expected_exceptions=[
            ExpectedException(record_id="setl_A|BNK-1", reason_code="AMBIGUOUS_DUPLICATE_AMOUNT"),
            ExpectedException(record_id="setl_B|BNK-2", reason_code="AMBIGUOUS_DUPLICATE_AMOUNT"),
        ])
        sc = compute_scorecard(report, gt, [])
        assert sc.exception_precision == 1.0

    def test_over_flagging_an_unambiguous_settlement_reduces_precision(self):
        report = make_report(
            ambiguous_groups=[AmbiguousGroup(settlement_ids=["setl_A", "setl_NOT_REALLY_AMBIGUOUS"],
                                              bank_txn_ids=["BNK-1", "BNK-2"], reason="test")],
        )
        gt = make_ground_truth([], expected_exceptions=[
            ExpectedException(record_id="setl_A|BNK-1", reason_code="AMBIGUOUS_DUPLICATE_AMOUNT"),
        ])
        sc = compute_scorecard(report, gt, [])
        assert sc.exception_precision == 0.5


class TestLLMStats:
    def test_llm_stats_pulled_correctly_from_p5_records(self):
        p5 = P5Result(
            matched={"setl_A": "BNK-1"},
            records=[
                AdjudicationRecord(
                    bank_txn_id="BNK-1", candidate_settlement_ids=["setl_A"],
                    raw_decision=AdjudicationDecision.MATCH, raw_candidate_id="setl_A",
                    raw_confidence=0.95, reasoning="x", evidence=[],
                    accepted_match=("setl_A", "BNK-1"), rejection_reason=None,
                    prompt_hash="h1", latency_ms=100.0, from_cache=False,
                ),
                AdjudicationRecord(
                    bank_txn_id="BNK-2", candidate_settlement_ids=["setl_B", "setl_C"],
                    raw_decision=AdjudicationDecision.ESCALATE, raw_candidate_id=None,
                    raw_confidence=0.3, reasoning="ambiguous", evidence=[],
                    accepted_match=None, rejection_reason=None,
                    prompt_hash="h2", latency_ms=100.0, from_cache=False,
                ),
                AdjudicationRecord(
                    bank_txn_id="BNK-3", candidate_settlement_ids=["setl_D"],
                    raw_decision=AdjudicationDecision.MATCH, raw_candidate_id="setl_D",
                    raw_confidence=0.99, reasoning="x", evidence=[],
                    accepted_match=None, rejection_reason="arithmetic_mismatch",
                    prompt_hash="h3", latency_ms=100.0, from_cache=False,
                ),
            ],
        )
        report = make_report(settlement_to_bank_txn={"setl_A": "BNK-1"}, p5=p5)
        gt = make_ground_truth([])
        sc = compute_scorecard(report, gt, [make_settlement_summary("setl_A", 1), make_settlement_summary("setl_B", 1)])

        assert sc.n_llm_calls == 3
        assert sc.n_llm_accepted == 1
        assert sc.n_llm_escalated == 1
        assert sc.n_llm_rejected_by_guardrail == 1
        assert sc.llm_invocation_rate == 3 / 2
