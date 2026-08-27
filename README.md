# AI Finance Controller — Three-Way Settlement Reconciliation

> Razorpay AI Buildathon — Track 04
> Status: Days 1-2 — synthetic data generator complete, matching engine not yet built

## The problem

A Razorpay settlement lands as one lumped bank credit covering hundreds of
orders, net of MDR, 18% GST on that fee, and any refunds processed in the
same window. Reconciling that lump back to individual orders — proving,
order by order, that the arithmetic really adds up — is still mostly done by
hand.

<!-- TODO once built: 60-second demo instructions, results scorecard,
     architecture diagram, ablation table, honest limitations. See
     ARCHITECTURE.md and METRICS.md as they're written. -->

## What's here right now

- `src/recon/money.py` — paise-integer arithmetic core. Every amount in this
  codebase is an `int` number of paise, never a float.
- `src/recon/models.py` — Pydantic schemas for all three sources, mirroring
  Razorpay's real settlement recon report field names.
- `src/recon/normalise/dates.py` — shared business-day calendar (T+2 timing),
  used by both the generator and, later, the matcher's date-window tolerance.
- `src/recon/generate/` — the synthetic data generator:
  - `ledger.py` — Source A, the merchant's internal order ledger
  - `settlement.py` — Source B, the settlement recon report, kept
    consistent with the ledger (refunds, on-hold, chargebacks all trace back
    to a real order)
  - `bank.py` — Source C, deliberately the messiest: mangled UTRs, noise
    rows, a running balance as a self-consistency check
  - `anomalies.py` — the 18-case anomaly catalogue and its rate table
  - `ground_truth.py` — assembles labels from the generation process itself;
    never imported by anything except the (future) evaluator
  - `orchestrator.py` — ties it all together, writes `data/dev` (seed 42)
    and `data/test` (seed 1337)

Run the tests (44, covering money arithmetic, generator determinism, the
arithmetic-proof identity, bank-balance self-consistency, and that every
seeded anomaly actually appears):
```bash
pip install -e ".[dev]"
pytest -v
```

Generate the two datasets:
```bash
make generate
```
This produces, in each of `data/dev/` and `data/test/`: `internal_ledger.csv`,
`settlement_recon.json`, `settlement_summaries.json`, `bank_statement.csv`,
and `ground_truth.json` — six files per the roadmap's Day 1-2 milestone.

## What's next

See `FAILURE_LOG.md` for the running build diary — including a real bug
where negative-net settlement batches were silently zeroed out instead of
recorded as debits, caught by hand-checking the exact case the demo video is
built around, not by any test. Next up: the deterministic matching engine
(Days 3-5) — exact UTR join, ledger join, the arithmetic proof, fuzzy
matching and subset-sum for the residue.
