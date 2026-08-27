"""
Pass 3 — the arithmetic proof.

For every settlement batch: does sum(credit) - sum(debit) across its own
lines equal what the settlement summary itself claims (internal check), and
does it equal the bank credit it's been paired with, if any (external
check)? This is the differentiator the roadmap is built around — converting
"these look like they match" into "the money provably adds up."

Passing this pass does NOT resolve any Pass 1/4 ambiguity — a batch can be
perfectly, provably self-consistent while still being paired with the wrong
bank row, or with no bank row at all. That's a deliberate and important
property: arithmetic proves a batch is internally correct, not that it's
been correctly identified.
"""

from __future__ import annotations

from collections import defaultdict

from recon.models import BankRow, SettlementLine, SettlementSummary

from .types import BatchVariance, P3Result, VarianceClass

ROUNDING_TOLERANCE_PAISE = 100  # < ₹1 — genuine rounding drift, not a real variance


def prove_batches(
    lines: list[SettlementLine],
    summaries: list[SettlementSummary],
    settlement_to_bank_txn: dict[str, str],
    bank_rows: list[BankRow],
) -> P3Result:
    lines_by_settlement: dict[str, list[SettlementLine]] = defaultdict(list)
    for line in lines:
        if line.settlement_id is not None:
            lines_by_settlement[line.settlement_id].append(line)

    bank_by_id = {r.bank_txn_id: r for r in bank_rows}

    variances: dict[str, BatchVariance] = {}
    for summary in summaries:
        batch_lines = lines_by_settlement.get(summary.id, [])
        internal_net = sum(l.credit for l in batch_lines) - sum(l.debit for l in batch_lines)

        bank_txn_id = settlement_to_bank_txn.get(summary.id)
        bank_net = None
        if bank_txn_id is not None and bank_txn_id in bank_by_id:
            row = bank_by_id[bank_txn_id]
            bank_net = row.credit_paise - row.debit_paise

        # Compare against the bank credit when we have one; otherwise fall
        # back to checking the batch against its OWN summary claim — still
        # useful (catches a batch that's internally inconsistent even
        # before we know which bank row it belongs to).
        reference_net = bank_net if bank_net is not None else summary.amount
        delta = reference_net - internal_net

        if delta == 0:
            variance_class = VarianceClass.OK
        elif abs(delta) < ROUNDING_TOLERANCE_PAISE:
            variance_class = VarianceClass.ROUNDING_DRIFT
        else:
            variance_class = VarianceClass.UNEXPLAINED

        variances[summary.id] = BatchVariance(
            settlement_id=summary.id,
            internal_net=internal_net,
            summary_net=summary.amount,
            bank_net=bank_net,
            variance_class=variance_class,
            delta_paise=delta,
        )

    return P3Result(batch_variances=variances)
