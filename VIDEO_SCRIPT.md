# 5-Minute Video Script

Timing follows the roadmap's structure. Rehearse twice, record the third
take. Screen recording + voiceover — no talking head needed. Get to
running code by 1:00; don't open on slides about the payments industry.

Numbers below are your real dev evidence. **Before recording, swap in
your test numbers** wherever you see `[TEST: ...]` — everything else can
stay as written.

---

## 0:00–0:30 — Problem

**Show:** a bank statement row, or a mock-up of one — one line, one
amount, generic-looking narration.

**Say (adapt to your own voice):**

> "This one bank credit — ₹4,13,712.02 — is actually 20-something orders,
> netted against fees, GST on those fees, and a couple of refunds. Somebody
> at this merchant has to unpack that by hand every settlement cycle.
> That's the loop I'm closing: internal order ledger, Razorpay's own
> settlement report, and the bank statement — three sources, one proof."

---

## 0:30–1:00 — Approach

**Show:** the Mermaid diagram from `ARCHITECTURE.md` (render it, or just
show the file).

**Say:**

> "Five passes. Exact UTR match, then a ledger join, then an arithmetic
> proof — does the batch actually net to what it claims — then fuzzy
> matching for damaged references, and only then, for whatever's left, an
> LLM. The rule the whole thing is built around: deterministic code
> decides. The LLM only proposes, explains, and scores — and every
> proposal gets independently re-verified arithmetically before it's
> trusted. It never just takes the model's word."

---

## 1:00–1:45 — Run it

**Show:** terminal, running the real pipeline live.

```bash
python -m recon.match.engine data/dev
```

Let it run in real time — don't cut the wait. Land on the printed
summary.

**Say (while it runs, or right after):**

> "962 records, running in under a fifth of a second — that's the
> deterministic passes alone. [TEST: same speed on the held-out set — 968
> records, same order of magnitude.] Ninety-five point six percent fully
> resolved — order attribution, bank attribution, and the arithmetic
> proof, all three, provably correct. Zero unexplained variance anywhere
> in the batch."

---

## 1:45–2:45 — The hard case ⭐ (this is the beat to get right)

**Show:** open `results/dev_scorecard.json` or the exception output, find
the `AMBIGUOUS_DUPLICATE_AMOUNT` entry. Show the two bank rows side by
side — same date, same ₹4,13,712.02, and the narrations with no UTR in
either one.

**Say:**

> "Here's the case the whole design is really about. Two settlements,
> same net amount, same date. Deterministic matching correctly gives up —
> there's genuinely nothing in the data to tell them apart. It goes to the
> LLM, which sees exactly the same two candidates I do, with no other
> distinguishing information — and every real time I've run this, it says
> 'escalate.' Not a guess dressed up as confidence — an actual 'I can't
> tell.' That's not the model failing. Two settlements really are
> indistinguishable here, and correctly saying so is the right answer."

*(Optional, if time allows: show the `AdjudicationRecord` for this case —
confidence ~0.3-0.4, `rejection_reason: null`, meaning nothing had to
reject it — the model volunteered the right call on its own.)*

---

## 2:45–3:15 — Refusal

**Show:** the exception ledger sorted by rupees at risk (or the scorecard's
exception section: 4 flagged, 4 correctly so).

**Say:**

> "Four settlements out of thirty-nine end up in the exception list, sorted
> by money at risk, not by ID — and all four are genuinely ambiguous, not
> noise. A hundred percent exception precision. I'd rather a human look at
> four real judgment calls than have the system quietly guess on any of
> them."

---

## 3:15–4:00 — Honesty (the scorecard + the ablation)

**Show:** the full scorecard, then the ablation comparison table from
`METRICS.md`.

**Say:**

> "Zero false matches on every settlement with a known answer. But the
> number I actually care about more is this one: I ran the naive version —
> just send everything to the LLM, no deterministic passes, no arithmetic
> re-check — for real, on 62 records my pipeline already resolves cleanly.
> It didn't get any of them outright wrong either, at this sample size.
> What it did do: cost about two hundred times more per thousand records,
> and it produced friction — an escalate, an invalid response — on cases
> that never needed asking in the first place. That's the actual argument
> for verification-first design. Not 'the model is unreliable.' It's: you
> shouldn't have to ask, and asking has a real cost."

---

## 4:00–4:40 — What broke at 2AM

**Say (pick ONE real story — this one has the clearest arc):**

> "The one that got me: a settlement can legitimately net *negative* — a
> quiet day where refunds outweigh new sales. My first version just
> clamped that to a zero-value bank row instead of recording it as a debit.
> Nothing crashed. I only caught it because I went and hand-checked the
> exact pair my demo depends on, and found both bank credits showing
> ₹0.00. My own tests hadn't caught it, because they all checked internal
> consistency — did the batch net to what it claimed — not completeness.
> A green test suite told me the arithmetic was self-consistent. It didn't
> tell me the output was actually right."

*(Full write-up, and seven other real bugs from this build, in
`FAILURE_LOG.md` — mention it exists, don't read from it on camera.)*

---

## 4:40–5:00 — Limits, and close

**Say:**

> "Two honest limits. The amount-and-date fallback gets weaker, not
> stronger, as the dataset grows — one real run hit a coincidental
> three-way collision that had nothing to do with any seeded anomaly. And
> getting a working LLM connection took longer than the adjudication logic
> itself — a deprecated model, a silently-ignored parameter, two different
> rate limits, and an account restriction, all real, all in the failure
> log. With a real merchant account, the next thing I'd build is this same
> pipeline against live test-mode settlements instead of synthetic data."

---

## Recording checklist

- [ ] Test numbers swapped in for every `[TEST: ...]` placeholder
- [ ] Screen text legible at 720p (zoom your terminal font up first)
- [ ] Audio audible, no long silences during the live run (trim if needed —
      but don't cut the run itself, an instant "result" looks staged)
- [ ] Under 5:00 total
- [ ] Unlisted YouTube or Loom link, tested in an incognito window
