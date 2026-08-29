# Metrics

## Definitions

| Metric | Formula | Why it's here |
|---|---|---|
| **False match rate** ⭐ | 1 − (correct / all attempted matches) | The headline risk metric. A wrong match silently misstates the books — an unmatched record just waits for a human to look at it. Worse outcome, and it's the one that matters most in finance reconciliation specifically. |
| Match precision | correct / all attempted matches | The inverse framing of false match rate — included for readability. |
| Match rate | correct / ground-truth-askable total | Only computed over settlements where a definite correct answer exists (ground truth excludes the genuinely ambiguous case by design — see `ARCHITECTURE.md`). |
| Value-weighted match rate | ₹ correctly matched / ₹ total askable | Matching 99% of *records* but missing the one ₹8L settlement is a failure a count-weighted number would hide. |
| Exception precision | correctly-flagged-ambiguous / all-flagged-ambiguous | Are the exceptions real, or noise? A system that over-flags loses the trust of whoever triages the exception list. |
| LLM invocation rate | LLM-touched / total settlements | How much of the workload actually needed the expensive path. |
| Cost per 1,000 records | (real token counts × current pricing) / n × 1000 | Real, not assumed — see the token-tracking note below. |

## Scorecard — `data/dev`, real run

```
════════════════════════════════════════════════════════════
 Settlements                                39
 Settlement lines (records)                962

 BANK-ATTRIBUTION CORRECTNESS
   Ground truth askable                     35
   Correct matches                          35
   False matches                             0   star
   Declined (no answer given)                0
   Match rate                           100.0%
   Match precision                      100.0%
   False match rate                       0.0%   star
   Value-weighted match rate            100.0%

 EXCEPTION QUALITY
   Flagged ambiguous                         4
   ...of which correctly so                  4
   Exception precision                  100.0%

 LLM LAYER (Pass 5, real residue calls)
   Calls made                                5
   Accepted                                  0
   Escalated (correctly declined)            5
   Rejected by guardrail                     0
   Invocation rate                       12.8%   (of the 39 settlements -
                                                   see note below)
   Wall-clock                            0.14s
════════════════════════════════════════════════════════════
```

**Note on the 12.8% invocation rate:** this counts the 5 Pass-5 calls
against the 39 total settlements — i.e. "how many settlements needed the
LLM path at all," which is higher than the ~0.5% figure quoted in the
README (5 of 962 *records*). Both are correct; they answer different
questions — "what fraction of settlement *decisions* went to the LLM" vs.
"what fraction of individual order *records* were touched." The README
uses the record-level figure since it's the more conservative, more
directly cost-relevant one.

## Ablation — "LLM on everything" baseline, real run

Sampled 62 bank rows the deterministic pipeline already resolves cleanly
(seed-fixed, excludes rows already inside a known ambiguous group), gave
the model the full candidate list directly, and accepted its decision with
**no** confidence threshold, **no** hallucination check, and **no**
arithmetic re-verification — the point being to measure what happens
*without* the guardrails this project's real Pass 5 always applies.

```
════════════════════════════════════════════════════════════
 Sampled                    62
 Correct                    60
 False matches               0
 Escalated                   1
 Schema invalid               1
 False match rate         0.0%  (of attempted, non-escalated decisions)
 Tokens (in/out)     23,657 / 2,007
 Estimated cost          $0.0253  for 62 records
 Estimated cost / 1,000  $0.4076
════════════════════════════════════════════════════════════
```

**Reading this honestly:** at this sample size, the naive baseline did not
produce an outright false match either. The measured differentiator is
cost and reliability friction, not raw correctness at small scale — see
`README.md`'s "Why not just send everything to the LLM" section for the
full framing. The 1 escalate + 1 schema-invalid response, out of 62 cases
the real pipeline resolves with zero friction, is the more interesting
finding: touching every record costs you clean answers on cases that never
needed asking.

**Cost comparison, done fairly:** the real pipeline's Pass 5 shows
$0.0000 in this particular scorecard because those 5 calls were served
from cache (identical prompts to earlier runs). The fair comparison uses
the ablation's real per-call rate applied to Pass 5's actual invocation
count: 5 of 962 records (0.5%) at ~$0.0004/call = **$0.002 per 1,000
records**, versus the ablation's measured **$0.4076 per 1,000** — roughly
a **190x** difference, from asking far less often, not from being smarter
per call.

## Confidence calibration

Built from the ablation sample, not Pass 5's normal residue path — Pass 5
only ever sees genuinely ambiguous cases, where the objectively correct
answer is almost always `escalate`, which produces far too few actual
match/no_match decisions with known ground truth to calibrate against. The
ablation sample, by contrast, is drawn from records with a known true
answer.

```
════════════════════════════════════════════════════════════
 confidence range       n    accuracy    well-calibrated?
 0.95-1.01             60     100.0%                 yes
════════════════════════════════════════════════════════════
```

Every confident (0.95+) prediction in this sample was correct. A larger
sample would give more buckets to check — at n=62 with 60 landing in one
bucket, this is suggestive rather than a fully resolved calibration curve.

## Determinism

The deterministic pipeline (Passes 1-4) is fully deterministic by
construction — same input, same seed, same output, always (verified by
`tests/test_generate.py::TestDeterminism`). The LLM layer's determinism is
weaker: Gemini's sampling parameters (`temperature`/`top_p`/`top_k`) are
deprecated and silently ignored as of Gemini 3.6 Flash, so bit-for-bit
repeatability across separate real API calls is not something client-side
settings can guarantee on that model generation. The prompt cache
(`llm_cache.py`) makes *within a run* behavior consistent regardless.

## Dev to test — real result

```
════════════════════════════════════════════════════════════
 Settlements                                38   (dev: 39)
 Settlement lines (records)                968   (dev: 962)

 Ground truth askable                       34   (dev: 35)
 Correct matches                            34   (dev: 35)
 False matches                               0   (dev: 0)
 Match rate                             100.0%   (dev: 100.0%)
 False match rate                         0.0%   (dev: 0.0%)
 Value-weighted match rate              100.0%   (dev: 100.0%)

 Flagged ambiguous                           4   (dev: 4)
 ...of which correctly so                    4   (dev: 4)
 Exception precision                    100.0%   (dev: 100.0%)

 LLM calls made                              4   (dev: 5)
 Escalated (correctly declined)              4   (dev: 5)
 Tokens (in/out, real, not cached)  2,012 / 570
 Cost / 1,000 records                   $0.0038
 Wall-clock                             50.94s   (real paced API calls,
                                                   not served from cache —
                                                   dev's near-instant times
                                                   reflected repeated runs
                                                   during development)
════════════════════════════════════════════════════════════
```

Per the held-out discipline maintained throughout this build: run exactly
once, same code and same anomaly rates as dev, only the seed differs (1337
vs 42). Identical headline numbers on both — 100% match rate, 0% false
matches, 100% exception precision — the closest possible dev/test
agreement.

**Read honestly, not triumphantly:** a 0% false match rate on both runs is
not primarily a generalization result — it's what Pass 3's arithmetic
proof structurally guarantees for anything it accepts, since a wrong match
would have to net incorrectly, which the proof catches by construction on
every run, by design, not by luck. The metric that genuinely does test
generalization is exception identification: does the system correctly
recognize genuine ambiguity rather than confidently guessing, on data it's
never seen before? Both dev and test say yes, on every single ambiguous
case (4 of 4, both runs) — that's the number worth trusting as evidence of
something that generalizes, not the 0%.
