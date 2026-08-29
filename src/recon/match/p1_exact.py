"""
Pass 1 — exact UTR join, settlement batch <-> bank credit row.

The first and cheapest pass: does a UTR extracted from the bank narration
(or its ref_no field) match a known settlement UTR EXACTLY? If more than one
DIFFERENT settlement's UTR shows up as a candidate for the same bank row (or
vice versa), that's a genuine ambiguity, and this pass's job is to notice it
and refuse, not guess.

Critically, "the same UTR appears on more than one bank row" is NOT
automatically the same failure mode as "two different settlements collide."
A settlement's genuine payout credit and an erroneous duplicate of that same
credit (seeded anomaly #13) will legitimately share one UTR across two bank
rows — but they're both CREDITS, both for the SAME settlement, and one of
them is simply a data-quality problem sitting next to a perfectly good
match. Filtering candidates by sign (credit vs debit) BEFORE judging
ambiguity is what keeps a reversal debit — which incidentally carries the
same reference — from being treated as a competing candidate at all.
"""

from __future__ import annotations

from collections import defaultdict

from recon.models import BankRow, SettlementSummary
from recon.normalise.utr import extract_utr_candidates

from .types import AmbiguousGroup, P1Result


def match_exact_utr(summaries: list[SettlementSummary], bank_rows: list[BankRow]) -> P1Result:
    utr_to_settlement: dict[str, str] = {s.utr: s.id for s in summaries}
    settlement_by_id = {s.id: s for s in summaries}
    bank_by_id = {r.bank_txn_id: r for r in bank_rows}

    def row_sign(row: BankRow) -> int:
        return 1 if row.credit_paise > row.debit_paise else -1

    def settlement_sign(sid: str) -> int:
        return 1 if settlement_by_id[sid].amount >= 0 else -1

    # bank_txn_id -> set of settlement_ids whose UTR appears as a candidate
    # in that row's narration/ref_no (direction-agnostic at this stage)
    bank_candidates: dict[str, set[str]] = {}
    for row in bank_rows:
        if row.credit_paise <= 0 and row.debit_paise <= 0:
            continue  # a zero-value row shouldn't exist, but skip defensively
        candidates = extract_utr_candidates(row.narration, row.ref_no)
        hits = {utr_to_settlement[c] for c in candidates if c in utr_to_settlement}
        if hits:
            bank_candidates[row.bank_txn_id] = hits

    # invert: settlement_id -> set of bank_txn_ids that named it, split by
    # whether the row's direction actually matches the settlement's own
    # expected direction (positive-net settlement -> credit; negative-net
    # -> debit). Only same-direction rows compete for "which one is THE
    # payout" — opposite-direction rows sharing a UTR are a different thing
    # entirely (a reversal, a correction) and must not contaminate this
    # pass's ambiguity judgment.
    settlement_same_sign: dict[str, set[str]] = defaultdict(set)
    for bank_txn_id, settlement_ids in bank_candidates.items():
        row = bank_by_id[bank_txn_id]
        for sid in settlement_ids:
            if row_sign(row) == settlement_sign(sid):
                settlement_same_sign[sid].add(bank_txn_id)

    matched: dict[str, str] = {}
    duplicate_same_sign: dict[str, list[str]] = {}
    ambiguous: list[AmbiguousGroup] = []
    resolved_bank_ids: set[str] = set()
    resolved_settlement_ids: set[str] = set()

    # Detect cross-settlement conflicts PER BANK ROW first, once — not per
    # settlement. Checking from each settlement's own side independently
    # (an earlier version did this) finds the same conflict twice, once
    # from each participant, and emits a duplicate AmbiguousGroup for a
    # single underlying collision.
    conflicted_bank_ids: set[str] = set()
    for bank_id, candidate_settlement_ids in bank_candidates.items():
        row = bank_by_id[bank_id]
        same_sign_settlements = {
            sid for sid in candidate_settlement_ids
            if bank_id in settlement_same_sign.get(sid, set())
        }
        if len(same_sign_settlements) > 1:
            ambiguous.append(AmbiguousGroup(
                settlement_ids=sorted(same_sign_settlements),
                bank_txn_ids=[bank_id],
                reason="one bank row carries same-direction candidates for multiple settlements",
            ))
            resolved_settlement_ids.update(same_sign_settlements)
            resolved_bank_ids.add(bank_id)
            conflicted_bank_ids.add(bank_id)

    for sid, bank_ids in settlement_same_sign.items():
        if sid in resolved_settlement_ids:
            continue  # already accounted for by a cross-settlement conflict above
        bank_ids = bank_ids - conflicted_bank_ids
        if not bank_ids:
            continue

        if len(bank_ids) == 1:
            bank_id = next(iter(bank_ids))
            matched[sid] = bank_id
            resolved_settlement_ids.add(sid)
            resolved_bank_ids.add(bank_id)
        else:
            # Same settlement, same UTR, more than one same-direction bank
            # row: pick the earliest (bank_txn_id order == chronological
            # order, per the generator) as the real match, and record the
            # rest as duplicates — a distinct, resolvable finding, not a
            # reason to leave the settlement unmatched.
            ordered = sorted(bank_ids)
            matched[sid] = ordered[0]
            duplicate_same_sign[sid] = ordered[1:]
            resolved_settlement_ids.add(sid)
            resolved_bank_ids.update(bank_ids)

    all_settlement_ids = {s.id for s in summaries}
    all_nonzero_bank_ids = {
        r.bank_txn_id for r in bank_rows if r.credit_paise > 0 or r.debit_paise > 0
    }

    return P1Result(
        matched=matched,
        unresolved_settlement_ids=sorted(all_settlement_ids - resolved_settlement_ids),
        unresolved_bank_txn_ids=sorted(all_nonzero_bank_ids - resolved_bank_ids),
        ambiguous=ambiguous,
        duplicate_same_sign=duplicate_same_sign,
    )