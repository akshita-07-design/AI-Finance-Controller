# AI Finance Controller — Three-Way Settlement Reconciliation

> Razorpay AI Buildathon — Track 04

Closes one finance-ops loop end to end: a merchant's internal order ledger
↔ Razorpay's settlement recon report ↔ the bank statement. Every settlement
is either resolved with a proven, arithmetically-verified bank credit, or
correctly identified as genuinely ambiguous and left for a human — never
guessed.

## Results (dev, real Gemini/Anthropic runs — see note below on `test`)

```
════════════════════════════════════════════════════════════
 SETTLEMENT-LEVEL (where bank attribution actually happens)
════════════════════════════════════════════════════════════
 Total settlement batches                  39
 Have a definite, provable answer          35   (ground truth "askable")
   Correctly resolved                      35   (100% match precision)
   False matches                            0   ← headline risk metric
 Genuinely ambiguous by construction         4   (identical amount+date,
                                                   zero UTR signal either side)
   Correctly identified as ambiguous         4   (100% exception precision)

 → All 39 settlements handled correctly: resolved where a correct answer
   exists, declined where it genuinely doesn't. Zero false matches, zero
   wrongly-forced guesses.

════════════════════════════════════════════════════════════
 RECORD-LEVEL ROLL-UP (962 individual order-linked lines)
════════════════════════════════════════════════════════════
 Fully resolved (order + bank + arithmetic, all three)   95.6%
 Unexplained arithmetic variance                          0
 Records inside a correctly-flagged ambiguous batch     ~4.4%
                                                        (proportionate to
                                                         the anomaly's
                                                         designed rarity)

════════════════════════════════════════════════════════════
 LLM LAYER (Pass 5 — only the residue Passes 1-4 decline to resolve)
════════════════════════════════════════════════════════════
 Calls made on real dev residue              5   (0.5% invocation rate)
 Accepted                                     0
 Escalated — correctly declined              5
 Rejected by guardrail                        0

 → On this dataset, every residue case was genuinely unresolvable from the
   data alone (two settlements, identical amount and date, no UTR on
   either side) — and the real model's own judgment matched that, with
   zero guardrail intervention needed. See "the hard case" below.

[TEST — data/test, real run, seed 1337]
════════════════════════════════════════════════════════════
 SETTLEMENT-LEVEL
════════════════════════════════════════════════════════════
 Total settlement batches                  38
 Have a definite, provable answer          34
   Correctly resolved                      34   (100% match precision)
   False matches                            0
 Genuinely ambiguous by construction         4
   Correctly identified as ambiguous         4   (100% exception precision)

 LLM LAYER
   Calls made                                4   (10.5% of settlements)
   Accepted                                   0
   Escalated — correctly declined             4
   Tokens (in/out, real — not cached)  2,012 / 570
   Cost / 1,000 records                  $0.0038

 Dev and test land on identical headline numbers (100% match rate, 0%
 false matches, 100% exception precision) — the closest possible dev/test
 agreement, meaning nothing here was tuned against dev. Worth noting
 honestly: a 0% false match rate isn't purely luck — it's structurally
 guaranteed for anything that clears Pass 3's arithmetic proof, since a
 wrong match would have to net incorrectly, which the proof catches by
 construction. The harder thing to get right consistently — and the part
 that actually tests generalization — is correctly identifying genuine
 ambiguity rather than guessing. Both runs got that right on every single
 ambiguous case (4/4 in both dev and test).
════════════════════════════════════════════════════════════
```

## 60-second demo

```bash
git clone <this-repo>
cd razorpay-finance-controller
pip install -e ".[dev]"
pytest -v                              # 99 passed (+3 more if Anthropic client tested too)
make generate                          # writes data/dev, data/test
python -m recon.match.engine data/dev  # deterministic pipeline only
```

### LLM setup (produces every real number in this README/METRICS.md)

```bash
# Gemini — get a free key: https://aistudio.google.com/apikey (no card, no phone)
export GEMINI_API_KEY="your-key-here"
# PowerShell: [System.Environment]::SetEnvironmentVariable("GEMINI_API_KEY","your-key-here","User")
#
# This project's default model (gemini-2.5-flash) is blocked for new
# accounts ("no longer available to new users") — override with:
export GEMINI_MODEL="gemini-3.5-flash-lite"
# PowerShell: [System.Environment]::SetEnvironmentVariable("GEMINI_MODEL","gemini-3.5-flash-lite","User")

python scripts/test_llm_connection.py --provider gemini   # one real call, sanity check
python -m recon.evaluate.run_eval data/dev --llm --ablation 75 --provider gemini
```

Anthropic (Claude) is also fully implemented, guardrail-tested, and uses
real (not estimated) token counts — a separate signup at
console.anthropic.com gives ~$5 free credit, phone verification only, no
card. Swap `--provider gemini` for `--provider anthropic` anywhere above.

## Architecture

**Deterministic code decides. The LLM only proposes, explains, and scores.**
Five passes, in order:

1. **Exact UTR match** (`p1_exact.py`) — settlement batch ↔ bank row,
   sign-aware (a reversal debit never competes with a genuine credit for
   the same UTR — an earlier version got this wrong; see `FAILURE_LOG.md`)
2. **Ledger join** (`p2_ledger.py`) — settlement line ↔ internal order;
   refunds/adjustments follow `payment_id` back to the original payment,
   never trusting their own `order_receipt` field
3. **Arithmetic proof** (`p3_arithmetic.py`) — does each batch's own lines
   net to what the summary claims, and to the matched bank credit?
4. **Fuzzy + amount/date fallback** (`p4_fuzzy.py`, `p4_amount_date.py`) —
   confusion-aware UTR fuzzy matching, then a last-resort fallback for rows
   with zero UTR signal at all — this is where the flagship ambiguous case
   is actually caught (below)
5. **LLM adjudication** (`p5_llm.py`) — only the ~0.5-4% residue reaches
   here. The model can only choose a `candidate_id` from a list we supply
   (never emit an amount), and every accepted proposal is independently
   re-verified arithmetically before being trusted — never taken on the
   model's own word. `escalate` is a first-class, correct answer, not a
   fallback forced by schema constraints.

Full diagram and design alternatives considered (and rejected) in
`ARCHITECTURE.md`.

## The hard case

Two settlements, same net amount (₹4,13,712.02), same settlement date, and
— by design — zero recoverable UTR signal in either bank narration. This is
the single case the whole verification-first architecture is built around.

Passes 1 and 4 both correctly decline (there's genuinely nothing to
disambiguate on). It reaches the LLM, which is shown both candidates side
by side with no other distinguishing information — and on the real model,
across every real run this project made, it correctly says `escalate`
every time, at declared confidence around 0.3-0.4. No guardrail ever had to
catch a wrong guess here, because the model never guessed.

## Why not just send everything to the LLM

Ran the naive baseline for real (not simulated) on a sample of 62 bank
rows the deterministic pipeline already resolves cleanly:

| | Real pipeline (Pass 5, residue only) | Naive baseline (LLM on every record) |
|---|---|---|
| Records touched by the LLM | 5 of 962 (0.5%) | 62 of 62 (100%) |
| False matches | 0 | 0 |
| Escalated / schema-invalid | 5 / 0 | 1 / 1 |
| Cost per 1,000 records | ~$0.002 (extrapolated) | $0.4076 (measured) |

**Honest reading of this:** at this sample size, the naive baseline didn't
produce an outright wrong match either — the differentiator here isn't
"the LLM gets it wrong," it's that touching every record costs real money
and introduces friction (an escalate and a schema-invalid response) on
cases that never needed asking in the first place. Verification-first
design means you only pay — in dollars or in reliability risk — for the
~0.5% of records that are actually hard, not the other 99.5%. Full method,
including real API-provider constraints hit along the way, in
`FAILURE_LOG.md`.

## What I chose not to resolve

Every genuinely ambiguous case is logged with a reason code, sorted by
rupees at risk, not by ID — see `results/` for a real run's output.
`AMBIGUOUS_DUPLICATE_AMOUNT` is the only reason code this dataset actually
produces at scale; the exception ledger's schema supports others
(`VARIANCE_UNEXPLAINED`, `UTR_UNRECOVERABLE`, etc.) that a larger or
messier dataset would exercise.

## Data

Fully synthetic, generator included (`src/recon/generate/`). 18 anomaly
classes seeded with known ground truth and rate-configurable injection.
Dev/test split by seed (42 / 1337); `test` is run once, at the end — see
the placeholder above. Fee rates are plausible-realistic, not authoritative
Razorpay pricing.

## Honest limitations

- **The amount+date fallback weakens as the dataset grows.** One real run
  surfaced a group of 2 settlements against 3 bank rows — an unrelated bank
  row happened to share the exact same amount and date by pure
  coincidence, not by design. Handled correctly (all three escalated
  rather than two force-matched), but it's a live signal that a
  last-resort, information-free matching signal gets weaker, not stronger,
  as record counts increase. A `GROUP_SIZE_MISMATCH` reason code, distinct
  from genuine ambiguity, is the natural next refinement — not yet built.
- **LLM provider access was a real, multi-day constraint, not a footnote.**
  Built and tested against a fake client throughout, then hit, in order:
  a deprecated model name, a silently-ignored deprecated parameter, a
  5-request/minute rate limit, a 20-request/DAY quota specific to a brand-
  new model, and an account-level block on an older, more generous model
  ("no longer available to new users"). Switched to Anthropic, then found
  a viable Gemini alternative (`gemini-3.5-flash-lite`) after all. Every
  step is in `FAILURE_LOG.md`, because it's real engineering, not a
  distraction from it.
- **Cost figures for Pass 5 in a from-scratch run will show real tokens**,
  not the $0.0000 in the table above — that number reflects cache hits
  from repeated runs during development, not zero cost. The ablation
  table's per-call rate is the honest one to extrapolate from.
- **Token estimation for Gemini is a text-length heuristic, not a metered
  count** — Gemini's `usage_metadata` field is documented as unsupported on
  the direct API path this project uses. Anthropic's token counts, by
  contrast, are real metered values from the API response.

## What broke → `FAILURE_LOG.md`

Real, dated entries — most caught by tracing one specific record all the
way through rather than trusting a metric that looked plausible. Includes
the ambiguous-duplicate anomaly's blast radius being wrong in *both*
directions before it was right, a negative-net settlement batch silently
zeroed instead of recorded as a debit, and the LLM-provider saga above.
