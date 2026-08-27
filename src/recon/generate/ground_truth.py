"""
Assembles ground truth from the generation process itself.

This module is never imported by the matching engine — only by the
evaluator. That separation is the whole point of a held-out test: the
matcher only ever sees the three raw sources, never this file's output,
until scoring time.
"""

from __future__ import annotations

from recon.generate.anomalies import AnomalyRates, ReasonCode
from recon.generate.bank import BankGenResult
from recon.generate.ledger import OrderPlan
from recon.generate.settlement import SettlementGenResult
from recon.models import (
    ExpectedException,
    GeneratorConfig,
    GroundTruth,
    GroundTruthMatch,
    MatchType,
)

_FILLER_MARKER = "Synthetic filler"  # matches settlement.py's filler line description


def build_ground_truth(
    seed: int,
    month: str,
    n_orders: int,
    rates: AnomalyRates,
    plans: list[OrderPlan],
    settlement_result: SettlementGenResult,
    bank_result: BankGenResult,
) -> GroundTruth:
    receipt_to_internal: dict[str, str] = {
        p.ledger.order_receipt: p.ledger.internal_order_id for p in plans
    }

    def first_bank_txn(settlement_id: str | None) -> str | None:
        if settlement_id is None:
            return None
        txns = bank_result.settlement_to_bank_txn.get(settlement_id, [])
        return txns[0] if txns else None

    matches: list[GroundTruthMatch] = []
    match_counter = 0

    for line in settlement_result.lines:
        if line.description and _FILLER_MARKER in line.description:
            # Purely synthetic collision filler — has no ledger counterpart
            # by construction. The batch-level ambiguity it creates is
            # captured separately, below, as an expected exception.
            continue

        internal_order_id = (
            receipt_to_internal.get(line.order_receipt) if line.order_receipt else None
        )
        bank_txn_id = first_bank_txn(line.settlement_id)

        match_type = (
            MatchType.THREE_WAY_EXACT if internal_order_id else MatchType.SETTLEMENT_BANK_ONLY
        )
        match_counter += 1
        matches.append(GroundTruthMatch(
            match_id=f"m_{match_counter:05d}",
            internal_order_id=internal_order_id,
            settlement_entity_id=line.entity_id,
            settlement_id=line.settlement_id,
            bank_txn_id=bank_txn_id,
            match_type=match_type,
            anomaly_tags=line.anomaly_tags,
        ))

    expected_exceptions: list[ExpectedException] = []

    # Seeded anomaly #12 — the ambiguous-duplicate-amount pair. Both sides
    # are genuinely unresolvable from the bank's perspective alone; a
    # correct system should decline, not guess.
    for settlement_id_a, settlement_id_b in settlement_result.duplicate_amount_pairs:
        bank_a = first_bank_txn(settlement_id_a)
        bank_b = first_bank_txn(settlement_id_b)
        expected_exceptions.append(ExpectedException(
            record_id=f"{settlement_id_a}|{bank_a}",
            reason_code=ReasonCode.AMBIGUOUS_DUPLICATE_AMOUNT,
            anomaly_tags=["seeded_12_ambiguous_duplicate_amount"],
            note=(
                f"Same net amount and settlement date as {settlement_id_b} "
                f"(bank {bank_b}); UTR deliberately unrecoverable in both "
                "bank narrations."
            ),
        ))
        expected_exceptions.append(ExpectedException(
            record_id=f"{settlement_id_b}|{bank_b}",
            reason_code=ReasonCode.AMBIGUOUS_DUPLICATE_AMOUNT,
            anomaly_tags=["seeded_12_ambiguous_duplicate_amount"],
            note=(
                f"Same net amount and settlement date as {settlement_id_a} "
                f"(bank {bank_a}); UTR deliberately unrecoverable in both "
                "bank narrations."
            ),
        ))

    # Seeded anomaly #13 — a duplicated credit row that is later reversed.
    # Flagged on the duplicate credit row itself (not the original, not the
    # reversal) since that's the row whose naive inclusion would overstate
    # revenue if not cross-referenced against its reversal.
    for row in bank_result.rows:
        if "seeded_13_duplicate_reversed" in row.anomaly_tags and row.credit_paise > 0:
            expected_exceptions.append(ExpectedException(
                record_id=row.bank_txn_id,
                reason_code=ReasonCode.VARIANCE_UNEXPLAINED,
                anomaly_tags=["seeded_13_duplicate_reversed"],
                note="Duplicate credit row, reversed the following day — must not be double-counted.",
            ))

    return GroundTruth(
        generator_config=GeneratorConfig(
            seed=seed, month=month, n_orders=n_orders, anomaly_rates=rates.as_dict(),
        ),
        matches=matches,
        expected_exceptions=expected_exceptions,
    )
