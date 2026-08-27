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

<!--
Your next entries go here. Real candidates you'll likely hit in the next few
days, per the roadmap:
  - the generator producing a running bank balance that doesn't reconcile
    with itself
  - GST rounding remainders you didn't expect
  - refund matching on date proximity instead of following payment_id
  - subset-sum returning more than one valid answer
Write them when they happen, in your own words, including the wrong turn
before the right one — that's the part that actually reads as credible.
-->
