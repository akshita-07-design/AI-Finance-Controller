"""
Confidence calibration.

Deliberately built against the ABLATION sample's records, not Pass 5's
normal residue path. Pass 5 only ever sees the ~4% of genuinely ambiguous
cases, where the objectively correct answer is almost always "escalate" —
that path produces very few actual match/no_match decisions with known
ground truth to calibrate against. The ablation sample, by contrast, is
drawn from records the deterministic passes already resolve cleanly, so we
know the true answer for each one — which is exactly what a calibration
curve needs: many (confidence, correct/incorrect) pairs to bucket.

A well-calibrated model's 0.9-confidence predictions should be right about
90% of the time. If they're right only 60% of the time, the model is
overconfident — worth knowing before trusting its confidence score to gate
anything in production.
"""

from __future__ import annotations

from dataclasses import dataclass

from recon.evaluate.ablation import AblationRecord


@dataclass
class CalibrationBucket:
    label: str            # e.g. "0.8-0.9"
    lower: float
    upper: float
    n_predictions: int = 0
    n_correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.n_correct / self.n_predictions if self.n_predictions else 0.0


DEFAULT_BUCKET_EDGES = [0.0, 0.5, 0.7, 0.8, 0.9, 0.95, 1.01]  # 1.01 so confidence=1.0 falls inside the last bucket


def compute_calibration(
    records: list[AblationRecord],
    bucket_edges: list[float] | None = None,
) -> list[CalibrationBucket]:
    """Only records where the model actually committed to match/no_match
    (not escalate, not schema_invalid) carry a meaningful confidence score
    to calibrate — an escalation has no 'was it right' answer in the same
    sense a positive claim does."""
    edges = bucket_edges or DEFAULT_BUCKET_EDGES
    buckets = [
        CalibrationBucket(label=f"{edges[i]:.2f}-{edges[i+1]:.2f}", lower=edges[i], upper=edges[i + 1])
        for i in range(len(edges) - 1)
    ]

    for r in records:
        if r.outcome not in ("correct", "false_match") or r.raw_confidence is None:
            continue
        for bucket in buckets:
            if bucket.lower <= r.raw_confidence < bucket.upper:
                bucket.n_predictions += 1
                if r.outcome == "correct":
                    bucket.n_correct += 1
                break

    return buckets


def format_calibration(buckets: list[CalibrationBucket]) -> str:
    lines = [
        "=" * 60,
        " CONFIDENCE CALIBRATION (from the ablation sample)",
        "=" * 60,
        f" {'confidence range':<18}{'n':>6}{'accuracy':>12}{'well-calibrated?':>20}",
    ]
    any_data = False
    for b in buckets:
        if b.n_predictions == 0:
            continue
        any_data = True
        midpoint = (b.lower + b.upper) / 2
        # "well-calibrated" here just means accuracy roughly tracks the
        # bucket's own midpoint — a rough visual check, not a formal metric
        gap = abs(b.accuracy - midpoint)
        verdict = "yes" if gap < 0.15 else ("overconfident" if b.accuracy < midpoint else "underconfident")
        lines.append(f" {b.label:<18}{b.n_predictions:>6}{b.accuracy:>11.1%}{verdict:>20}")
    if not any_data:
        lines.append(" (no match/no_match decisions with confidence scores to calibrate — "
                      "all sampled records escalated or failed schema validation)")
    lines.append("=" * 60)
    return "\n".join(lines)
