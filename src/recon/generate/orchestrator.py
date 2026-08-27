"""
Orchestrates the full generation pipeline: ledger -> settlement -> bank ->
ground truth, and writes everything to disk.

Run directly:  python -m recon.generate.orchestrator
Produces data/dev (seed 42) and data/test (seed 1337) from the SAME anomaly
rates and order count — only the seed differs. If dev and test look
different in ways beyond seed noise, that's a bug in this file, not in the
matcher you'll build against it.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from recon.generate.anomalies import AnomalyRates
from recon.generate.bank import generate_bank_statement
from recon.generate.ground_truth import build_ground_truth
from recon.generate.ledger import generate_ledger
from recon.generate.settlement import generate_settlements

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MONTH = "2026-04"
DEFAULT_N_ORDERS = 1000


def generate_dataset(seed: int, month: str, n_orders: int, out_dir: Path,
                      rates: AnomalyRates | None = None) -> dict:
    """Generate one complete dataset (ledger + settlement + bank + ground
    truth) and write it to `out_dir`. Returns a small stats dict for the
    caller to print or assert on."""
    rates = rates or AnomalyRates()
    rng = random.Random(seed)

    plans = generate_ledger(rng, month, n_orders, rates)
    settlement_result = generate_settlements(rng, plans, rates)

    force_ids = frozenset(
        sid for pair in settlement_result.duplicate_amount_pairs for sid in pair
    )
    bank_result = generate_bank_statement(
        rng, settlement_result.summaries, rates, month,
        force_unrecoverable_utr_ids=force_ids,
    )

    ground_truth = build_ground_truth(
        seed, month, n_orders, rates, plans, settlement_result, bank_result,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Source A: internal ledger (CSV — this is what a merchant's own
    #     order-management export would look like) ---
    ledger_df = pd.DataFrame([p.ledger.model_dump(mode="json") for p in plans])
    ledger_df.to_csv(out_dir / "internal_ledger.csv", index=False)

    # --- Source B: settlement recon report (JSON — mirrors the real API
    #     response shape). Includes unsettled (on-hold) lines too, since a
    #     merchant genuinely sees these in their settlement view even
    #     though they haven't paid out yet. ---
    all_settlement_lines = settlement_result.lines + settlement_result.unsettled_lines
    with open(out_dir / "settlement_recon.json", "w") as f:
        json.dump(
            {"entity": "collection", "count": len(all_settlement_lines),
             "items": [l.model_dump(mode="json") for l in all_settlement_lines]},
            f, indent=2,
        )

    # --- Settlement batch summaries — smaller, separate endpoint in the
    #     real API (`settlement->fetch`). Cheap to include, and Pass 3's
    #     arithmetic proof needs it directly. ---
    with open(out_dir / "settlement_summaries.json", "w") as f:
        json.dump(
            [s.model_dump(mode="json") for s in settlement_result.summaries],
            f, indent=2,
        )

    # --- Source C: bank statement (CSV — this is how a bank actually
    #     exports it) ---
    bank_df = pd.DataFrame([r.model_dump(mode="json") for r in bank_result.rows])
    bank_df.to_csv(out_dir / "bank_statement.csv", index=False)

    # --- Ground truth — never read by the matcher, only by the evaluator ---
    with open(out_dir / "ground_truth.json", "w") as f:
        json.dump(ground_truth.model_dump(mode="json"), f, indent=2)

    # --- quick stats for the caller to print / assert on ---
    anomaly_counts: dict[str, int] = {}
    for line in all_settlement_lines:
        for tag in line.anomaly_tags:
            anomaly_counts[tag] = anomaly_counts.get(tag, 0) + 1
    for row in bank_result.rows:
        for tag in row.anomaly_tags:
            anomaly_counts[tag] = anomaly_counts.get(tag, 0) + 1

    return {
        "n_orders": n_orders,
        "n_ledger_records": len(plans),
        "n_settlement_lines": len(all_settlement_lines),
        "n_settlement_batches": len(settlement_result.summaries),
        "n_bank_rows": len(bank_result.rows),
        "n_ground_truth_matches": len(ground_truth.matches),
        "n_expected_exceptions": len(ground_truth.expected_exceptions),
        "anomaly_counts": anomaly_counts,
    }


def main():
    rates = AnomalyRates()
    print(f"Generating dev (seed=42) and test (seed=1337), {DEFAULT_N_ORDERS} orders each...\n")

    dev_stats = generate_dataset(
        seed=42, month=DEFAULT_MONTH, n_orders=DEFAULT_N_ORDERS,
        out_dir=REPO_ROOT / "data" / "dev", rates=rates,
    )
    test_stats = generate_dataset(
        seed=1337, month=DEFAULT_MONTH, n_orders=DEFAULT_N_ORDERS,
        out_dir=REPO_ROOT / "data" / "test", rates=rates,
    )

    for label, stats in [("DEV (seed=42)", dev_stats), ("TEST (seed=1337)", test_stats)]:
        print(f"--- {label} ---")
        for k, v in stats.items():
            if k != "anomaly_counts":
                print(f"  {k}: {v}")
        print("  anomaly_counts:")
        for tag, count in sorted(stats["anomaly_counts"].items()):
            print(f"    {tag}: {count}")
        print()


if __name__ == "__main__":
    main()
