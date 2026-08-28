# AI Finance Controller — Three-Way Settlement Reconciliation

> Razorpay AI Buildathon — Track 04
> Status: Days 6-7 — LLM adjudication layer complete, guardrails fully tested

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

- `src/recon/money.py` — paise-integer arithmetic core.
- `src/recon/models.py` — Pydantic schemas for all three sources, mirroring
  Razorpay's real settlement recon report field names.
- `src/recon/normalise/` — shared business-day calendar, UTR extraction
  cascade + confusion-aware canonicalization, and loaders that read the
  three on-disk sources back into the same models (this is the matcher's
  ONLY view of the data — no generation-time metadata survives past here).
- `src/recon/generate/` — the synthetic data generator (ledger, settlement,
  bank, 18-anomaly catalogue, ground truth, orchestrator).
- `src/recon/match/` — the matching engine:
  - `p1_exact.py` — exact UTR join, settlement batch ↔ bank row, with
    sign-aware ambiguity detection (a reversal debit must never compete
    with a genuine credit for the same UTR)
  - `p2_ledger.py` — settlement line ↔ ledger order join; refunds and
    adjustments follow `payment_id` back to the original payment rather
    than trusting their own `order_receipt`
  - `p3_arithmetic.py` — the arithmetic proof: does each batch's own lines
    net to what the summary claims, and to the matched bank credit?
  - `p4_fuzzy.py` / `p4_amount_date.py` — confusion-aware fuzzy UTR
    matching, then a last-resort amount+date fallback for rows with zero
    UTR signal at all (this is where the ambiguous-duplicate-amount anomaly
    is actually caught — genuine ambiguity, correctly deferred, not guessed)
  - `engine.py` — orchestrates all passes, classifies every record along
    two independent dimensions (order attribution vs. bank attribution)

Run the tests (64, covering money arithmetic, the generator's invariants,
and the matcher's core behaviors — including regression tests for eight
real bugs found while building this against real generated data; see
FAILURE_LOG.md):
```bash
pip install -e ".[dev]"
pytest -v
```

Generate data and run the matcher:
```bash
make generate
python -m recon.match.engine data/dev
python -m recon.match.engine data/test
```

**Current headline numbers:** 95.6% (dev) / 95.7% (test) fully resolved
(order attribution + bank attribution + arithmetic proof, all three), 0
unexplained arithmetic variance in either dataset, ~4.4% genuinely ambiguous
in both (proportionate to the anomaly's designed rarity). Dev and test are
close together — the small remaining gap is real signal, not an artifact of
tuning against dev.

## What's next

See `FAILURE_LOG.md` for the running build diary. The Days 3-5 entry alone
covers eight real bugs — most caught by tracing one specific record all the
way through rather than trusting a metric that looked plausible. Next up:
the LLM adjudication layer (Days 6-7) for the small residue (~4%) that
deterministic passes correctly decline to resolve — with independent
arithmetic re-verification of every proposal before acceptance.

## LLM adjudication (Days 6-7)

Central thesis: **deterministic code decides, the LLM only proposes,
explains, and scores.** Only the residue Passes 1-4 correctly decline to
resolve (~4% of records) ever reaches the model, and every accepted
proposal is independently re-verified arithmetically before it's trusted —
never taken on the model's own word.

- `src/recon/match/p5_llm.py` — the adjudicator. The model can only choose
  a `candidate_id` from a list we supply (never emit an amount — there's no
  field for one), and that id is checked against the offered list before
  being trusted at all. `escalate` is a first-class decision: when the
  evidence is genuinely ambiguous (this project's flagship case — two
  settlements with the identical amount and date, no UTR signal on either
  side), correctly declining is the right answer, not a failure.
- `src/recon/match/llm_cache.py` — a prompt→response cache keyed by
  `sha256(prompt)`, so re-running the pipeline never re-calls the API for a
  question already answered (temperature=0 makes this a valid optimization,
  not a correctness risk).
- `tests/test_p5_llm.py` — 11 tests covering every guardrail, using a fake
  LLM client that returns scripted responses (including deliberately broken
  ones: hallucinated IDs, malformed JSON, confident-but-wrong arithmetic).
  This is fully testable without network access or an API key — see the
  module docstring for why that split matters.

**Setup** (see `scripts/test_llm_connection.py` for a one-call sanity check
before trusting the full pipeline):
```bash
# 1. Get a free key: https://aistudio.google.com/apikey
# 2. Set it as an environment variable — NEVER hardcode it in source:
export GEMINI_API_KEY="your-key-here"          # macOS/Linux
# or, PowerShell:  [System.Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "your-key-here", "User")

python scripts/test_llm_connection.py           # one real call, sanity check
python -m recon.match.engine data/dev --llm     # full pipeline with adjudication
```

**One structural limit, stated plainly rather than hidden:** arithmetic
re-verification proves a proposed pairing is *consistent* — it cannot prove
a pairing is the unique *correct* one when several candidates are equally
consistent, which is exactly this project's ambiguous-duplicate case (every
candidate settlement has the identical amount by construction). That's why
the confidence threshold and the `escalate` option exist as independent
defenses, not folded into the arithmetic check. See `p5_llm.py`'s module
docstring and the Days 6-7 entry in `FAILURE_LOG.md` for the full reasoning,
including a real finding: one ambiguous group came back as 2 settlements
against 3 bank rows — an unrelated bank row coincidentally sharing the
exact same amount and date, purely by chance, correctly handled by
escalating all three rather than force-matching two and guessing on the
third.

## What's next after that

Day 8: the evaluation harness — the full scorecard (match rate, false match
rate, value-weighted match rate), a confidence calibration curve, and the
ablation table comparing this 5-pass design against "LLM on everything."
