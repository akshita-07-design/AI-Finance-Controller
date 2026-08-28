"""Tests for confidence calibration, using hand-built AblationRecord fixtures."""

from __future__ import annotations

from recon.evaluate.ablation import AblationRecord
from recon.evaluate.calibration import compute_calibration


def make_record(confidence, outcome):
    return AblationRecord(
        bank_txn_id="BNK-1", raw_decision=None, raw_candidate_id=None,
        raw_confidence=confidence, true_settlement_id=None, outcome=outcome,
    )


class TestCalibration:
    def test_perfectly_calibrated_bucket(self):
        # 10 predictions at ~0.9 confidence, 9 correct = 90% accuracy —
        # exactly matching the bucket's own claim.
        records = [make_record(0.92, "correct") for _ in range(9)] + [make_record(0.91, "false_match")]
        buckets = compute_calibration(records)
        bucket_90 = next(b for b in buckets if b.label == "0.90-0.95")
        assert bucket_90.n_predictions == 10
        assert bucket_90.accuracy == 0.9

    def test_escalated_and_schema_invalid_records_are_excluded(self):
        """Only actual match/no_match decisions carry a meaningful
        confidence-vs-correctness pairing — an escalation isn't 'right' or
        'wrong' in the same sense."""
        records = [
            make_record(0.95, "escalated"),
            make_record(None, "schema_invalid"),
            make_record(0.9, "correct"),
        ]
        buckets = compute_calibration(records)
        total_counted = sum(b.n_predictions for b in buckets)
        assert total_counted == 1

    def test_overconfident_model_is_detectable(self):
        """A model claiming 0.95+ confidence but right only half the time
        is exactly the failure mode calibration exists to catch."""
        records = (
            [make_record(0.97, "correct") for _ in range(5)]
            + [make_record(0.96, "false_match") for _ in range(5)]
        )
        buckets = compute_calibration(records)
        bucket_95 = next(b for b in buckets if b.label == "0.95-1.01")
        assert bucket_95.accuracy == 0.5

    def test_empty_bucket_has_zero_accuracy_not_a_crash(self):
        buckets = compute_calibration([])
        for b in buckets:
            assert b.accuracy == 0.0
            assert b.n_predictions == 0
