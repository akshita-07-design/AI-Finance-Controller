"""
The matching engine — ties Passes 1-4 together and classifies every record.

Deliberately produces TWO separate resolution dimensions per settlement
line, not one collapsed status:

  1. Order attribution (Pass 2): do we know which internal order this line
     belongs to?
  2. Bank attribution (Pass 1/4/3): do we know which specific bank
     transaction represents this line's settlement batch, and does the
     arithmetic prove it?

These usually coincide, but don't have to — the ambiguous-duplicate case
(seeded #12) is exactly a scenario where order attribution can be perfect
while bank attribution is correctly, irreducibly unresolved. Collapsing both
into one status would either wrongly penalize a correct order-level match or
wrongly excuse a genuine bank-level exception. See ground_truth.py for the
same split applied to the evaluation labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from recon.match.p1_exact import match_exact_utr
from recon.match.p2_ledger import link_to_ledger
from recon.match.p3_arithmetic import prove_batches
from recon.match.p4_amount_date import resolve_amount_and_date
from recon.match.p4_fuzzy import resolve_fuzzy
from recon.match.types import (
    AmbiguousGroup,
    OrderLinkMethod,
    P1Result,
    P2Result,
    P3Result,
    P4Result,
    VarianceClass,
)
from recon.models import SettlementEntityType, SettlementLine
from recon.normalise.loaders import DataSources


@dataclass
class RecordClassification:
    entity_id: str
    order_link_method: OrderLinkMethod
    order_resolved: bool
    bank_ambiguous: bool
    variance_class: VarianceClass | None   # None if the batch has no bank match at all
    fully_resolved: bool                    # order_resolved AND not ambiguous AND variance OK/rounding


@dataclass
class MatchReport:
    p1: P1Result
    p4: P4Result
    p2: P2Result
    p3: P3Result
    classifications: dict[str, RecordClassification] = field(default_factory=dict)
    ambiguous_groups: list[AmbiguousGroup] = field(default_factory=list)


def run_matching(data_dir: Path) -> MatchReport:
    sources = DataSources(data_dir)

    settled_lines = [l for l in sources.settlement_lines if l.settled]
    unsettled_lines = [l for l in sources.settlement_lines if not l.settled]

    p1 = match_exact_utr(sources.settlement_summaries, sources.bank_rows)
    p4_fuzzy = resolve_fuzzy(p1, sources.settlement_summaries, sources.bank_rows)
    p4 = resolve_amount_and_date(p1, p4_fuzzy, sources.settlement_summaries, sources.bank_rows)

    settlement_to_bank_txn = {**p1.matched, **p4.matched}
    ambiguous_settlement_ids = {
        sid for group in p4.ambiguous for sid in group.settlement_ids
    }

    p2 = link_to_ledger(sources.settlement_lines, sources.ledger)

    p3 = prove_batches(
        settled_lines, sources.settlement_summaries, settlement_to_bank_txn, sources.bank_rows,
    )

    classifications: dict[str, RecordClassification] = {}

    for line in unsettled_lines:
        # on-hold payments: never in a batch, nothing to prove arithmetically
        # — order attribution still applies normally.
        order_resolved = p2.order_link.get(line.entity_id) is not None
        classifications[line.entity_id] = RecordClassification(
            entity_id=line.entity_id,
            order_link_method=p2.link_method[line.entity_id],
            order_resolved=order_resolved,
            bank_ambiguous=False,
            variance_class=None,
            fully_resolved=order_resolved,  # on-hold is a resolved CLASSIFICATION, not an exception
        )

    for line in settled_lines:
        method = p2.link_method[line.entity_id]
        order_resolved = method in (OrderLinkMethod.DIRECT_RECEIPT,
                                     OrderLinkMethod.PAYMENT_ID_CHAIN,
                                     OrderLinkMethod.NONE_EXPECTED)

        is_ambiguous = line.settlement_id in ambiguous_settlement_ids
        # A settlement can fail to resolve to ANY bank row without being
        # flagged ambiguous — e.g. a UTR truncated mid-string by a real
        # narration length limit, leaving a partial candidate too different
        # to fuzzy-match but not "zero signal" either, so it never reaches
        # the amount+date fallback. Missing this check silently counted such
        # a line as resolved: p3's fallback compares an unmatched batch
        # against its OWN summary claim (self-consistent by construction),
        # so variance_ok was True even though we never actually identified
        # which bank transaction represents this settlement's payout.
        bank_resolved = line.settlement_id in settlement_to_bank_txn
        variance = p3.batch_variances.get(line.settlement_id)
        variance_class = variance.variance_class if variance else None
        variance_ok = variance_class in (VarianceClass.OK, VarianceClass.ROUNDING_DRIFT)

        fully_resolved = order_resolved and bank_resolved and not is_ambiguous and variance_ok

        classifications[line.entity_id] = RecordClassification(
            entity_id=line.entity_id,
            order_link_method=method,
            order_resolved=order_resolved,
            bank_ambiguous=is_ambiguous,
            variance_class=variance_class,
            fully_resolved=fully_resolved,
        )

    return MatchReport(
        p1=p1, p4=p4, p2=p2, p3=p3,
        classifications=classifications,
        ambiguous_groups=p4.ambiguous,
    )


def print_summary(report: MatchReport, sources: DataSources | None = None) -> None:
    total = len(report.classifications)
    fully_resolved = sum(1 for c in report.classifications.values() if c.fully_resolved)
    order_resolved = sum(1 for c in report.classifications.values() if c.order_resolved)
    ambiguous_records = sum(1 for c in report.classifications.values() if c.bank_ambiguous)
    variance_unexplained = sum(
        1 for c in report.classifications.values()
        if c.variance_class == VarianceClass.UNEXPLAINED
    )

    print(f"Total settlement lines classified: {total}")
    print(f"  Fully resolved (order + bank + arithmetic): {fully_resolved} "
          f"({fully_resolved/total:.1%})")
    print(f"  Order-level resolved:                       {order_resolved} "
          f"({order_resolved/total:.1%})")
    print(f"  In an ambiguous bank-pairing group:          {ambiguous_records}")
    print(f"  Batches with unexplained variance:           {variance_unexplained}")
    print()
    print(f"Pass 1 (exact UTR):      {len(report.p1.matched)} settlements matched, "
          f"{len(report.p1.duplicate_same_sign)} with a flagged duplicate credit, "
          f"{len(report.p1.unresolved_settlement_ids)} unresolved, "
          f"{len(report.p1.ambiguous)} ambiguous groups")
    p4_only_matched = len(report.p4.matched)
    print(f"Pass 4 (fuzzy+amount/date): {p4_only_matched} additional settlements matched, "
          f"{len(report.p4.still_unresolved_settlement_ids)} still unresolved, "
          f"{len(report.p4.ambiguous)} ambiguous groups")


if __name__ == "__main__":
    import sys
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/dev")
    report = run_matching(data_dir)
    print_summary(report)
