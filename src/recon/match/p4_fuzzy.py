"""
Pass 4 (fuzzy half) — confusion-aware fuzzy UTR matching for whatever Pass 1
couldn't resolve exactly.

Only fires on the residue: bank rows with no exact UTR match, and
settlements with no exact bank match. Accepts a fuzzy match only when BOTH
of two independent signals agree — canonical-edit-distance closeness AND an
exact amount match. One signal alone is a guess; two agreeing is evidence
(roadmap Part 5.5). If more than one settlement is within threshold and
matches on amount, that's the genuine ambiguity case (seeded #12) — defer,
don't pick one.
"""

from __future__ import annotations

from rapidfuzz.distance import Levenshtein

from recon.models import BankRow, SettlementSummary
from recon.normalise.utr import canonicalize, extract_utr_candidates

from .types import AmbiguousGroup, P1Result, P4Result, SettlementBankMatchMethod

MAX_CANONICAL_EDIT_DISTANCE = 2


def resolve_fuzzy(
    p1: P1Result,
    summaries: list[SettlementSummary],
    bank_rows: list[BankRow],
) -> P4Result:
    summaries_by_id = {s.id: s for s in summaries}
    bank_by_id = {r.bank_txn_id: r for r in bank_rows}

    unresolved_summaries = [summaries_by_id[sid] for sid in p1.unresolved_settlement_ids]
    unresolved_bank_rows = [bank_by_id[bid] for bid in p1.unresolved_bank_txn_ids]

    matched: dict[str, str] = {}
    method: dict[str, SettlementBankMatchMethod] = {}
    ambiguous: list[AmbiguousGroup] = []
    resolved_settlement_ids: set[str] = set()
    resolved_bank_ids: set[str] = set()

    canonical_utr_by_settlement = {s.id: canonicalize(s.utr) for s in unresolved_summaries}

    for row in unresolved_bank_rows:
        row_net = row.credit_paise - row.debit_paise
        candidates = extract_utr_candidates(row.narration, row.ref_no)
        if not candidates:
            continue  # nothing UTR-shaped to even compare — stays unresolved

        # Best canonical distance from ANY extracted candidate to each
        # unresolved settlement's UTR.
        close_settlement_ids: list[str] = []
        for sid, canon_utr in canonical_utr_by_settlement.items():
            if sid in resolved_settlement_ids:
                continue
            summary = summaries_by_id[sid]
            if summary.amount != row_net:
                continue  # amount must agree exactly — the second signal
            best_distance = min(
                Levenshtein.distance(canonicalize(c), canon_utr) for c in candidates
            )
            if best_distance <= MAX_CANONICAL_EDIT_DISTANCE:
                close_settlement_ids.append(sid)

        if len(close_settlement_ids) == 1:
            sid = close_settlement_ids[0]
            matched[sid] = row.bank_txn_id
            method[sid] = SettlementBankMatchMethod.FUZZY_UTR
            resolved_settlement_ids.add(sid)
            resolved_bank_ids.add(row.bank_txn_id)
        elif len(close_settlement_ids) > 1:
            # Two or more settlements are both close in UTR AND exact in
            # amount — genuinely ambiguous. This is where the seeded #12
            # collision actually gets caught, since Pass 1 couldn't even
            # extract a clean UTR for either twin.
            ambiguous.append(AmbiguousGroup(
                settlement_ids=sorted(close_settlement_ids),
                bank_txn_ids=[row.bank_txn_id],
                reason="multiple settlements match on amount within fuzzy UTR distance",
            ))
            resolved_settlement_ids.update(close_settlement_ids)
            resolved_bank_ids.add(row.bank_txn_id)

    still_unresolved_settlement_ids = sorted(
        set(p1.unresolved_settlement_ids) - resolved_settlement_ids
    )
    still_unresolved_bank_txn_ids = sorted(
        set(p1.unresolved_bank_txn_ids) - resolved_bank_ids
    )

    return P4Result(
        matched=matched,
        match_method=method,
        still_unresolved_settlement_ids=still_unresolved_settlement_ids,
        still_unresolved_bank_txn_ids=still_unresolved_bank_txn_ids,
        ambiguous=ambiguous,
    )
