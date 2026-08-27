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
