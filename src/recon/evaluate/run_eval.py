"""
Day 8 evaluation entry point.

Usage:
    python -m recon.evaluate.run_eval data/dev --llm
    python -m recon.evaluate.run_eval data/dev --llm --ablation 75
    python -m recon.evaluate.run_eval data/test --llm     # run ONCE, at the end

This is the only place the ablation's real, quota-consuming Gemini calls
happen — deliberately not part of `pytest` or any automated check, since it
should be a conscious choice each time, not something that fires on every
test run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from recon.evaluate.ablation import (
    format_ablation_result,
    run_ablation,
    sample_scoreable_bank_rows,
)
from recon.evaluate.calibration import compute_calibration, format_calibration
from recon.evaluate.metrics import compute_scorecard, format_scorecard
from recon.match.engine import run_matching
from recon.match.llm_cache import PromptCache
from recon.normalise.loaders import DataSources, load_ground_truth


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m recon.evaluate.run_eval <data_dir> [--llm] [--ablation N]")
        sys.exit(1)

    data_dir = Path(sys.argv[1])
    use_llm = "--llm" in sys.argv
    ablation_n = 0
    if "--ablation" in sys.argv:
        idx = sys.argv.index("--ablation")
        ablation_n = int(sys.argv[idx + 1])

    llm_client = None
    if use_llm:
        from recon.match.p5_llm import GeminiClient
        llm_client = GeminiClient()

    print(f"Running matching engine on {data_dir}" + (" (with LLM adjudication)" if use_llm else "") + "...")
    start = time.monotonic()
    report = run_matching(
        data_dir, llm_client=llm_client,
        llm_cache_path=Path("results") / f"{data_dir.name}_llm_cache.json",
    )
    elapsed = time.monotonic() - start

    sources = DataSources(data_dir)
    ground_truth = load_ground_truth(data_dir / "ground_truth.json")

    scorecard = compute_scorecard(report, ground_truth, sources.settlement_summaries)
    print()
    print(format_scorecard(scorecard))
    print()
    print(f"Wall-clock: {elapsed:.2f}s ({scorecard.total_records / elapsed:.0f} records/sec)")

    results_payload = {
        "data_dir": str(data_dir),
        "used_llm": use_llm,
        "wall_clock_seconds": elapsed,
        "scorecard": {k: v for k, v in vars(scorecard).items()},
    }

    if ablation_n > 0:
        if llm_client is None:
            print("\n--ablation requires --llm (need a real client to run it against)")
            sys.exit(1)
        print(f"\nRunning ablation baseline on a sample of up to {ablation_n} bank rows...")
        sample, truth_map = sample_scoreable_bank_rows(sources.bank_rows, ground_truth, n=ablation_n)
        ablation_cache = PromptCache(Path("results") / f"{data_dir.name}_ablation_cache.json")
        ablation_result = run_ablation(sample, truth_map, sources.settlement_summaries, llm_client, cache=ablation_cache)
        print()
        print(format_ablation_result(ablation_result))
        print()
        print(f"Comparison: real pipeline false match rate = {scorecard.false_match_rate:.1%}  "
              f"vs. ablation ('LLM on everything') false match rate = {ablation_result.false_match_rate:.1%}")
        print(f"Comparison: real pipeline LLM cost/1,000 records = "
              f"${(scorecard.llm_estimated_cost_usd / scorecard.total_records * 1000) if scorecard.total_records else 0:.4f}  "
              f"vs. ablation cost/1,000 records = "
              f"${(ablation_result.estimated_cost_usd / ablation_result.n_sampled * 1000) if ablation_result.n_sampled else 0:.4f}")
        print()
        calibration_buckets = compute_calibration(ablation_result.records)
        print(format_calibration(calibration_buckets))
        results_payload["ablation"] = {
            "n_sampled": ablation_result.n_sampled,
            "n_correct": ablation_result.n_correct,
            "n_false_matches": ablation_result.n_false_matches,
            "n_escalated": ablation_result.n_escalated,
            "false_match_rate": ablation_result.false_match_rate,
            "input_tokens": ablation_result.input_tokens,
            "output_tokens": ablation_result.output_tokens,
            "estimated_cost_usd": ablation_result.estimated_cost_usd,
        }
        results_payload["calibration"] = [
            {"bucket": b.label, "n": b.n_predictions, "accuracy": b.accuracy}
            for b in calibration_buckets if b.n_predictions > 0
        ]

    Path("results").mkdir(exist_ok=True)
    out_path = Path("results") / f"{data_dir.name}_scorecard.json"
    with open(out_path, "w") as f:
        json.dump(results_payload, f, indent=2, default=str)
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    main()
