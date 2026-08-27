"""
Pass 4 (amount+date half) — the last-resort fallback for settlements and
bank rows that carry NO usable UTR signal at all (zero candidates
extractable from narration or ref_no).

This is deliberately the weakest signal in the whole engine: same net amount,
same settlement date. Accepted ONLY when it's unique — exactly one
settlement and one bank row share that (amount, date) pair. The moment two
or more settlements (or two or more bank rows) share the same (amount, date)
with no UTR to break the tie, that's the genuine irreducible ambiguity this
project is built around (seeded anomaly #12) — the correct behavior is to
refuse, not to guess based on which one happened to be iterated first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from recon.models import BankRow, SettlementSummary
from recon.normalise.utr import extract_utr_candidates

from .types import AmbiguousGroup, P1Result, P4Result, SettlementBankMatchMethod


def resolve_amount_and_date(
    p1: P1Result,
    fuzzy: P4Result,
    summaries: list[SettlementSummary],
    bank_rows: list[BankRow],
) -> P4Result:
    summaries_by_id = {s.id: s for s in summaries}
    bank_by_id = {r.bank_txn_id: r for r in bank_rows}

    remaining_settlement_ids = fuzzy.still_unresolved_settlement_ids
    remaining_bank_ids = fuzzy.still_unresolved_bank_txn_ids

    # Only rows/settlements with ZERO UTR-shaped candidates belong here.
    # Anything that still has a candidate but failed fuzzy matching is a
    # different problem (UTR_UNRECOVERABLE, not amount+date ambiguity) and
    # should NOT be silently rescued by the weaker fallback.
    def has_no_utr_signal_bank(bank_txn_id: str) -> bool:
        row = bank_by_id[bank_txn_id]
        return len(extract_utr_candidates(row.narration, row.ref_no)) == 0

    eligible_bank_ids = [bid for bid in remaining_bank_ids if has_no_utr_signal_bank(bid)]
    eligible_settlement_ids = list(remaining_settlement_ids)  # settlements have no "narration" of their own

    groups: dict[tuple[int, int], list[str]] = defaultdict(list)   # (amount, date_ordinal) -> settlement_ids
    bank_groups: dict[tuple[int, int], list[str]] = defaultdict(list)

    for sid in eligible_settlement_ids:
        s = summaries_by_id[sid]
        d = datetime.fromtimestamp(s.created_at, tz=timezone.utc).date()
        groups[(s.amount, d.toordinal())].append(sid)

    for bid in eligible_bank_ids:
        r = bank_by_id[bid]
        net = r.credit_paise - r.debit_paise
        # Use value_date, not txn_date, as the join key here — value_date is
        # the field that actually corresponds to the settlement's own
        # settle date; txn_date can be shifted a day later by the value-date
        # skew anomaly (#11), which would otherwise wrongly break this
        # already-weak fallback even further.
        bank_groups[(net, r.value_date.toordinal())].append(bid)

    matched: dict[str, str] = {}
    method: dict[str, SettlementBankMatchMethod] = {}
    ambiguous: list[AmbiguousGroup] = []
    resolved_settlement_ids: set[str] = set()
    resolved_bank_ids: set[str] = set()

    for key, settlement_ids in groups.items():
        bank_ids = bank_groups.get(key, [])
        if not bank_ids:
            continue
        if len(settlement_ids) == 1 and len(bank_ids) == 1:
            sid, bid = settlement_ids[0], bank_ids[0]
            matched[sid] = bid
            method[sid] = SettlementBankMatchMethod.FUZZY_UTR  # weakest tier, same family
            resolved_settlement_ids.add(sid)
            resolved_bank_ids.add(bid)
        else:
            # >=2 on either side, same amount, same date, no UTR to break
            # the tie — genuine, irreducible ambiguity.
            ambiguous.append(AmbiguousGroup(
                settlement_ids=sorted(settlement_ids),
                bank_txn_ids=sorted(bank_ids),
                reason="identical amount and settlement date, no UTR signal on either side",
            ))
            resolved_settlement_ids.update(settlement_ids)
            resolved_bank_ids.update(bank_ids)

    return P4Result(
        matched={**fuzzy.matched, **matched},
        match_method={**fuzzy.match_method, **method},
        still_unresolved_settlement_ids=sorted(
            set(remaining_settlement_ids) - resolved_settlement_ids
        ),
        still_unresolved_bank_txn_ids=sorted(
            set(remaining_bank_ids) - resolved_bank_ids
        ),
        ambiguous=[*fuzzy.ambiguous, *ambiguous],
    )
