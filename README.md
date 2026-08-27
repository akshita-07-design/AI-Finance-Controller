# AI Finance Controller — Three-Way Settlement Reconciliation

> Razorpay AI Buildathon — Track 04
> Status: Day 0 — foundation laid, generator not yet built

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
  codebase is an `int` number of paise, never a float. See the module
  docstring for why.
- `tests/test_money.py` — 22 tests, including the netting identity
  (`credit = gross − fee − tax`) that Pass 3 of the matching engine will
  later rely on to prove a settlement batch against a bank credit.

Run them:
```bash
pip install -e ".[dev]"
pytest -v
```

## What's next

See `FAILURE_LOG.md` for the running build diary, and the roadmap for the
full day-by-day plan: synthetic data generator (Days 1-2, the most important
component), deterministic matching engine (Days 3-5), LLM adjudication layer
(Days 6-7), evaluation and ablation (Day 8), report and video (Days 9-11).
