# Failure Log

What broke, what I believed at the time, what was actually true, how I found it,
and what changed. Written as it happens — not retrofitted at the end.

Format:
```
### DATE, TIME — one-line summary

**Symptom:** what you observed
**What I believed:** your hypothesis at the time
**What was actually true:** the real cause
**How I found it:** the actual debugging step, not the tidy version
**Fix:** what changed
**What I'd do differently:** the transferable lesson
```

---

### 2026-08-27 — `bool` is secretly an `int` in Python, and money code has to guard against it explicitly

**Symptom:** Writing `assert_is_paise()` to reject anything that isn't a real
paise integer, the obvious check was `isinstance(value, int)`. Looked
complete.

**What I believed:** an `isinstance(x, int)` check is sufficient to guarantee
"this is a whole number of paise."

**What was actually true:** in Python, `bool` is a subclass of `int` —
`isinstance(True, int)` is `True`, and `True == 1`. So a bug anywhere upstream
that accidentally hands a boolean into a money field (e.g. a pandas column
that got coerced, or a dict `.get(key, False)` default meant for a flag field
that got wired to the wrong column) would silently pass validation as "1
paise" instead of raising. That's exactly the kind of silent, wrong-answer
failure this whole project exists to prevent — an assertion meant to catch
bad money data would wave one specific bad case straight through.

**How I found it:** not a real bug yet — caught by asking "what's the most
Python-specific way this check could lie to me" before writing the test, then
writing `test_rejects_bool` to confirm the suspicion. It failed on the first
version of `assert_is_paise`.

**Fix:** added an explicit `isinstance(value, bool)` check *before* the `int`
check, so a bool is rejected with a clear message instead of silently
accepted as an integer.

**What I'd do differently:** nothing to redo here — but the general lesson is
worth carrying forward: for every "type guard," ask what technically
satisfies the check but semantically shouldn't. This is the same instinct
that'll matter later for the UTR regex (a bank reference number could
technically match the same pattern as something that isn't a UTR at all) and
for the subset-sum matcher (a mathematically valid subset isn't automatically
the *correct* one).

---

### 2026-08-27 — First generator run, four bugs in ~20 minutes

Ran the generator for the first time end to end. Four real bugs surfaced, in
order:

**1. `_weighted_choice` assumed the wrong tuple shape.**
`Symptom:` `ValueError: too many values to unpack (expected 2)` on the very
first order.
`Cause:` wrote one generic `_weighted_choice(rng, options)` helper expecting
`(item, weight)` pairs, then fed it `_VALUE_BANDS`, which are `(low, high,
weight)` triples for order-value ranges. `zip(*triples)` doesn't unpack into
two variables.
`Fix:` gave `_random_order_value` its own inline weighting logic instead of
forcing a 3-tuple through a 2-tuple-shaped helper.
`Lesson:` a "generic" helper used for two different tuple shapes is a bug
waiting to happen — should have just written two small functions instead of
one clever one.

**2. `date.fromtimestamp(...).replace(tzinfo=None)` — you can't set tzinfo on
a naive `date`.**
`Symptom:` `TypeError: 'tzinfo' is an invalid keyword argument for
replace()`.
`Cause:` confused `date` and `datetime` — only `datetime` carries a tzinfo
you can replace; `date.fromtimestamp()` also silently uses the *local*
timezone of whatever machine runs it, which would have made settlement dates
non-deterministic across my sandbox vs. a reviewer's laptop even if the
syntax had been valid.
`Fix:` `datetime.fromtimestamp(ts, tz=timezone.utc).date()` — explicit UTC,
no ambiguity, no reliance on the runtime's local timezone.
`Lesson:` anywhere a unix timestamp becomes a calendar date, name the
timezone explicitly. This is exactly the kind of bug that would only show up
on someone else's machine, at the worst possible time — before a submission
deadline, running on a judge's laptop instead of mine.

**3 & 4. Two related "which lines get the anomaly tag" bugs, both in the
anomaly-#12 (ambiguous duplicate amount) and #13 (duplicate reversed) logic.**
`Symptom:` nothing crashed — this is the more dangerous kind of bug. Every
real order line inside a batch that happened to collide with a synthetic
twin got tagged `seeded_12`, and both the ORIGINAL (legitimate) duplicate
credit row and its erroneous copy got tagged `seeded_13`.
`Why it matters:` this would have taught the evaluator, later, that dozens
of perfectly ordinary orders were "supposed to be ambiguous" just because
they happened to share a settlement batch with one synthetic collision —
and that a correct match on a clean original credit row was somehow
"expected to fail." Ground truth would have been quietly wrong in a way
that inflates apparent difficulty without testing anything real.
`Fix:` only the synthetic filler line (#12) and only the erroneous duplicate
row, not the original (#13), carry the anomaly tag.
`Lesson:` this is the generator-is-the-eval risk described in the roadmap,
made concrete: a "successful" run that produces plausible-looking numbers
can still be silently teaching the wrong lesson if the *labels* are wrong,
not just the data.

**5. The big one — negative-net settlement batches were silently zeroed out,
not recorded as debits.**
`Symptom:` inspecting the actual ambiguous-duplicate pair by hand (the exact
case the video is supposed to be built around), both bank rows showed
`credit_paise: 0`. The whole point of that pair is that they're identical
and nonzero.
`What I believed:` a settlement batch always pays the merchant money, so
`credit_paise = summary.amount` (clamped at zero, just in case) seemed safe.
`What was actually true:` a batch CAN net negative — a quiet day with a
handful of refunds and no new sales outweighs the fees on record. Real
Razorpay settlements can debit the merchant in exactly this situation. My
`max(summary.amount, 0)` didn't just round this to zero, it made a real
negative settlement day invisible in the bank statement entirely — a
mismatch between two of my own generated sources that no downstream matcher
could ever explain, because the source data itself was wrong.
`How I found it:` didn't find it by testing — found it by eyeballing the one
specific case the whole demo depends on. If I hadn't manually inspected that
exact pair, this would have shipped silently.
`Fix:` batches that net negative now produce a debit row of the correct
magnitude; batches that net positive produce a credit, as before. Also
restricted the anomaly-#12 injector to only draw from positive-net batches,
since a debit-day "duplicate" is a real case but a confusing, un-illustrative
one for the flagship anomaly the video is built around.
`Lesson, the one that matters most:` I only caught this because I checked
the ONE example the whole story depends on by hand, not because a test
caught it — the tests I'd written up to that point all asserted internal
consistency (does the batch net to what its own summary claims?), which this
bug didn't violate; the summary's `amount` field was correct throughout, only
the *translation of that amount into a bank row* was wrong. Passing tests
told me the arithmetic was self-consistent; they didn't tell me the output
was complete. Added a regression test afterward, but the real takeaway is:
for the two or three data points a demo is actually built around, look at
them directly — don't just trust that green tests mean the pipeline as a
whole is doing the right thing.

---

<!--
Next entries go here, once the matching engine (Days 3-5) is built:
  - the fuzzy-UTR matcher accepting a match on amount alone with no UTR signal
  - subset-sum returning more than one valid answer and picking the first
    one without noticing
  - a refund resolving to the wrong original payment because it matched on
    date proximity instead of following payment_id
-->

### 2026-08-27 — Building the matching engine: eight real bugs, one very long day

Built Passes 1-4 (exact UTR, ledger join, arithmetic proof, fuzzy + amount/
date fallback) and ran them against real generated data for the first time.
Unusually bug-dense session — writing this up properly because the pattern
across most of these bugs is the same one, and it's worth naming: **code
that looks correct in isolation can still be tested against the wrong
baseline**, and the only way I caught most of these was by tracing one
specific real record all the way through, not by trusting a metric that
looked plausible.

**1. Pass 1 didn't check credit/debit direction before judging ambiguity.**
A reversal debit (seeded anomaly #13) happened to carry the same ref_no as
its original credit and its duplicate. My first version of Pass 1 treated
"same UTR on 3 bank rows" as one big 3-way ambiguous group — correctly
identifying a problem, but the WRONG problem: a reversal isn't a competing
candidate for a settlement's payout, it's a different kind of event
entirely. Fix: filter candidates by sign (does the row's direction match the
settlement's expected direction?) before assessing ambiguity at all. This
also meant separating two failure modes I'd been conflating: "duplicate"
(we know exactly what happened, one extra copy of a good match) is not the
same thing as "ambiguous" (we genuinely can't tell which of several
different things this is).

**2 & 3. The ambiguous-duplicate-amount anomaly's blast radius, wrong in
both directions, twice.** First version: no batch-size limit on which
settlement could be chosen as the "original" side of the collision — one
run picked a 102-line and a 29-line batch, sweeping 14% of the whole dataset
into "bank attribution ambiguous" for an anomaly meant to represent two rare
collisions a month. Fixed with a hardcoded "≤4 lines" cutoff — which then
overcorrected to ZERO pairs ever firing, because this dataset's batches
average ~27 lines each; almost nothing was ever that small. Real fix: a
cutoff relative to THIS run's own batch-size distribution (bottom
quartile), not an arbitrary constant. Even then, a "shuffle among the
smallest few" step I'd added for variety turned out to have a pool-size
formula that, for small candidate counts, equaled the ENTIRE eligible set —
silently undoing the smallest-first sort and picking a 29-line batch just as
often as a 20-line one. Final fix: just take the smallest ones directly, no
shuffle. Three iterations to get one anomaly's rarity to actually mean what
it claims to mean.

**4. Ground truth was asserting information only the generator could know.**
Original design: every settlement line inside an ambiguous pair's "real"
batch got a definite `bank_txn_id` in ground truth, because the generator
obviously knows which one is "true." But a correctly-behaving matcher,
facing amount+date alone with no UTR signal, cannot distinguish the two —
declining is the RIGHT call, not a wrong one. Scoring that decline as an
error would penalize good behavior. Fix: for lines in an ambiguous batch,
ground truth now asserts order-level attribution only, and leaves
`bank_txn_id` unresolved — matching what's actually knowable from the data,
not what the generator happens to remember about its own construction.

**5. Two independently-generated UTRs, 12 edits apart, both matching on
amount — nearly broke the whole point of anomaly #12.** My first fuzzy-match
design used UTR-closeness as the primary signal, amount as secondary
corroboration. Checked by hand: the two twins' UTRs were completely
unrelated strings (edit distance 12), so each bank row's mangled UTR was
always closer to its OWN true settlement than to the other — meaning Pass 4
would have resolved both correctly via fuzzy matching alone, never even
reaching the ambiguity the anomaly was supposed to create. A mangled UTR is
not the same thing as a MISSING one. Fix: the force-unrecoverable case now
removes the UTR from the narration entirely (no digits, no candidate
extractable at all), forcing genuine reliance on amount+date — where the
real, irreducible ambiguity actually lives.

**6. A rigid regex broke UTR extraction entirely, not just exact matching.**
One UTR pattern requires exactly 10 consecutive digits. Mangling a SINGLE
digit inside that 10-digit prefix (e.g. `0`→`O` at position 4) breaks the
whole pattern match — extraction returned NOTHING, not a slightly-wrong
candidate, which then starved Pass 4's fuzzy matching of anything to compare
against. Fix: added a permissive fallback pattern (any 12-20 char alnum
blob) tried last, after the stricter ones, so mangled-but-recognizable UTRs
still produce a candidate for fuzzy comparison.

**7. Narration truncation losing part of the UTR by accident, not by
design.** Some template + UTR-style combinations run long enough that the
realistic 50-character truncation cuts INTO the UTR itself — nothing to do
with any of the 18 seeded anomalies, just template-length arithmetic. One
such case landed on a 101-line settlement batch and, combined with ref_no's
30%-chance-of-being-empty, produced a genuinely unresolvable record — which
silently dropped the TEST set's match rate ten points below DEV's for a
reason that had nothing to do with a real anomaly. Found by noticing dev and
test had drifted apart much more than they should have, then tracing the
exact unresolved record back to its bank row. Fix: whenever truncation
actually cuts into the UTR, ref_no now always carries the full value as a
guaranteed fallback. Real information loss should only ever come from
anomalies designed to cause it.

**8. The classification logic had a silent gap between "matched" and
"ambiguous."** A settlement that failed to resolve to ANY bank row — not
exact, not fuzzy, not amount+date, and NOT flagged ambiguous either — was
being counted as `fully_resolved = True`, because Pass 3's arithmetic proof
fell back to comparing the batch against its own summary claim (which is
trivially self-consistent) whenever no bank match existed. The bug this
uncovered (#7 above) is what exposed this one: even after fixing the
truncation issue, I only found this classification gap by asking "why did
`fully_resolved` stay true for a record I know wasn't actually matched to
anything."

**The one lesson that covers most of these:** a metric that looks reasonable
(95.7% match rate, 0 unexplained variance) can still be built on a
classification that's quietly wrong in one specific spot. The fixes that
mattered here came from tracing individual real records end-to-end — not
from any test that existed before I went looking. Several of the tests I
wrote AFTER catching these bugs (see tests/test_generate.py,
tests/test_match.py) would have caught them if they'd existed first; that's
the actual value of writing regression tests for bugs you found by hand,
even after the fact.

Final numbers after all of the above: **95.6% (dev) / 95.7% (test)** fully
resolved, 0 unexplained arithmetic variance in either, ~4.4% genuinely
ambiguous in both (proportionate to design), dev and test close together —
the dev/test gap this time is real signal, not an artifact.

<!--
Next entries go here, once the LLM adjudication layer (Days 6-7) is built:
  - the model proposing a match with no arithmetic backing
  - what happens when the model is given the escalate option vs. forced to
    choose match/no-match
  - calibration: does confidence 0.9 actually mean right 90% of the time
-->

### 2026-08-27 — Building the LLM adjudicator: a coincidental 3-way collision, and why the guardrails caught it safely anyway

Built Pass 5 (LLM adjudication), tested end to end using a fake client
(GEMINI_API_KEY isn't something I can test with directly in this
environment — the guardrail logic is fully covered by
tests/test_p5_llm.py using canned responses; the real API call itself has
to be verified by hand, on a machine with network access and a real key).

**Finding, not really a bug:** wiring Pass 5 into the full engine and
inspecting the actual ambiguous groups on real dev data, one group came back
as 2 settlements but **3 bank rows** — `['BNK-00012', 'BNK-00013',
'BNK-00014']` against only `['setl_..., setl_...]`. The seeded anomaly
(#12) only ever creates a clean 2-vs-2 collision by construction. This third
row is a genuine coincidence: some other bank row — plausibly a noise
row, or another settlement's bank credit — happened to share the *exact*
same net amount and settlement date as the injected pair, with no UTR
signal of its own either, so Pass 4's amount+date fallback swept it into
the same group.

This isn't dangerous, and it isn't really a bug in the sense the earlier
entries were — nothing is silently wrong. It's just a demonstration that a
weak, last-resort matching signal (amount+date alone) gets weaker, not
stronger, as a dataset grows: the birthday-paradox risk of an innocent
bystander coincidentally matching on the exact same two numbers goes up
with more batches and more days. The correct behavior here is exactly what
happened: with 2 settlements and 3 candidate bank rows, one bank row simply
cannot have a correct settlement pairing, and the adjudicator escalated all
three rather than force-matching two of them and guessing on the third.

**What I'd do differently if I had more time:** the amount+date fallback
could cross-check group SIZE before accepting even a "unique" 1-to-1 case —
right now it only refuses when *multiple* settlements or *multiple* bank
rows share a key, but it doesn't yet flag a size *mismatch* (2 settlements
vs 3 bank rows) as its own kind of anomaly worth a distinct reason code,
separate from "genuinely ambiguous pairing." Worth a `GROUP_SIZE_MISMATCH`
reason code in a future pass — noted here rather than built, given the
clock.

**What actually mattered today, confirmed by the fake-client tests:** every
guardrail fired exactly as designed —
- a hallucinated candidate_id (one never offered) is rejected outright
- a confident (0.99) match that doesn't arithmetically add up is rejected
  by our own re-check, not taken on the model's word
- confidence below threshold is treated as unresolved even when the
  decision itself says "match"
- escalation is recorded as CORRECT behavior, not a failure — the audit
  trail distinguishes "the model declined" from "we rejected what the model
  proposed," which matters when someone reviews this later and asks "did
  the AI actually try, or did it just refuse everything?"

The one structural limit worth stating plainly, and now written directly
into p5_llm.py's module docstring: arithmetic re-verification proves a
proposed pairing is *consistent*, not that it's *correct* when several
candidates are equally consistent — which is exactly this project's
flagship ambiguous case, where every candidate settlement has the identical
amount by construction. Verification catches wrong answers that don't add
up; it cannot, alone, catch a confident answer that adds up but is still
the wrong one. That's why the confidence threshold and the escalate option
exist as separate, independent defenses rather than being folded into the
arithmetic check.

<!--
Next entries go here, once evaluation (Day 8) is built:
  - the calibration curve — does confidence 0.9 actually mean right 90% of
    the time, once real Gemini responses replace the fake client's canned
    ones
  - the ablation table: LLM-on-everything vs the actual 5-pass pipeline
  - the false-match-rate headline number once real (not fake-escalate-only)
    LLM decisions are in the mix
-->

### 2026-08-28 — First real API call: wrong model name, and a silent-failure risk hiding right behind it

Ran `scripts/test_llm_connection.py` for the first time against the actual
Gemini API (had been building and testing everything up to this point
against a fake client — no network access from the dev sandbox this project
was mostly built in).

**Symptom:** `404 NOT_FOUND: This model models/gemini-2.0-flash is no
longer available.` — the model I'd hardcoded had been deprecated. Google's
own error message named the replacement directly: `gemini-3.6-flash`.

**The one-line fix was easy.** The more important thing was checking
whether anything ELSE about the API had changed before just swapping the
string and moving on — and it had. Gemini 3.6 Flash (and everything after
it) has deprecated `temperature`, `top_p`, and `top_k`. Critically, they
aren't rejected — **they're silently ignored.** The call still returns
`200 OK`. Nothing in the response, the SDK, or the docs surfaces this at
call time. Google's own migration notes are explicit that a future model
generation will start returning an HTTP 400 for these, which is the only
reason this is discoverable without a changelog: right now, the failure
mode is invisible.

**Why this mattered here specifically:** `p5_llm.py` set `temperature=0`
with a comment claiming it made the LLM call deterministic, which was the
stated justification for the prompt cache in `llm_cache.py` being a
correctness property and not just a cost optimization. That comment was
now simply false, and would have stayed false silently — no test I'd
written would ever have caught it, since the fake-client tests don't
exercise `GeminiClient` at all (that's the whole point of the fake-client
design), and a `200 OK` gives no signal that a parameter did nothing.

**Fix:** removed `temperature=0` entirely (it wasn't doing anything, and
future-proofing against the documented HTTP-400 warning is free). Rewrote
the comments in both `p5_llm.py` and `llm_cache.py` to state the honest,
current guarantee: the cache still saves cost and latency by never asking
the same question twice within a run, but it can no longer be described as
guaranteeing bit-for-bit reproducibility, because nothing at the client-
settings level can promise that anymore on this model generation.

**The lesson, stated plainly:** an unfamiliar SDK or API can change its
contract in ways that don't announce themselves as errors. A parameter that
used to matter can quietly stop mattering while the call keeps succeeding —
which is a more dangerous failure shape than a crash, because nothing forces
you to go looking for it. The only reason this got caught at all was
checking the *surrounding* migration notes after seeing an unrelated 404,
rather than just patching the one line the error message pointed at and
moving on.
