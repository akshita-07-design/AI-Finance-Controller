"""
The anomaly catalogue.

This is the single most important file in the generator. Every number this
project reports is only meaningful because these cases are deliberately
injected with a known label — see FAILURE_LOG.md and the roadmap for why.

Rates are expressed as a probability applied per-order (or as an absolute
count per month, where noted) and are tunable per-dataset — dev and test use
the SAME rates by design, only the seed differs, so a match-rate gap between
the two reflects overfitting to dev, not a harder test set.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Reason codes used in the exception ledger later — defined here because the
# generator is what decides which of these SHOULD apply to a given record.
class ReasonCode:
    AMBIGUOUS_DUPLICATE_AMOUNT = "AMBIGUOUS_DUPLICATE_AMOUNT"
    MISSING_IN_BANK = "MISSING_IN_BANK"
    MISSING_IN_SETTLEMENT = "MISSING_IN_SETTLEMENT"
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"
    UTR_UNRECOVERABLE = "UTR_UNRECOVERABLE"
    VARIANCE_UNEXPLAINED = "VARIANCE_UNEXPLAINED"
    FEE_MISMATCH = "FEE_MISMATCH"
    CROSS_CYCLE_UNRESOLVED = "CROSS_CYCLE_UNRESOLVED"
    ON_HOLD_PENDING = "ON_HOLD_PENDING"
    DISPUTE_ADJUSTMENT = "DISPUTE_ADJUSTMENT"
    MULTIPLE_SUBSET_SOLUTIONS = "MULTIPLE_SUBSET_SOLUTIONS"
    LLM_LOW_CONFIDENCE = "LLM_LOW_CONFIDENCE"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"


@dataclass
class AnomalyRates:
    """Per-order probabilities, unless the field name says '_per_month'.

    Defaults here match the roadmap's suggested rates (Part 3.5). Kept as a
    dataclass rather than a raw dict so a typo in a rate name fails fast with
    an AttributeError instead of silently doing nothing.
    """
    # order outcome shape
    p_abandoned: float = 0.12                 # #14 — never paid, must not become an exception
    p_upi: float = 0.15                       # #2 — zero fee, tests the rate table isn't hardcoded
    p_full_refund_same_cycle: float = 0.03    # #3
    p_partial_refund: float = 0.02            # #4
    p_cross_cycle_refund: float = 0.02        # #5 — refund lands weeks after the original payment
    p_on_hold: float = 0.015                  # #6 — captured, withheld, never settles
    p_chargeback_adjustment: float = 0.01     # #7 — dispute_id present, non-order entity
    p_amex: float = 0.01                      # #16 — highest MDR tier, tests rate table variance

    # settlement/bank-level anomalies, applied after orders are generated
    p_null_utr_adjustment: float = 0.005      # #8 — adjustment row with settlement_utr=None
    p_route_transfer_per_batch: float = 0.05  # #9 — vendor payout transfer debit, no order at all
    p_mangled_utr_in_narration: float = 0.03  # #10 — O<->0, truncation, applied per bank credit row
    p_value_date_skew: float = 0.04           # #11 — bank credit posts next business day

    # rare, count-based rather than rate-based — small denominators make a
    # probability meaningless, so we force an exact count into the month
    n_duplicate_amount_pairs_per_month: int = 2   # #12 — the ambiguous-duplicate killer case
    n_duplicate_bank_row_reversed_per_month: int = 1  # #13

    def as_dict(self) -> dict[str, float]:
        return {
            k: v for k, v in vars(self).items()
        }


# Card network / issuer pools purely for narrative realism in generated data
CARD_NETWORKS = ["Visa", "MasterCard", "RuPay", "Amex"]
CARD_ISSUERS = ["HDFC", "ICIC", "SBIN", "KARB", "UTIB", "PUNB"]

UTR_STYLE_WEIGHTS = {
    "digits_then_letters": 0.5,   # 1568176960vxp0rj
    "rzrp_prefixed": 0.3,         # RZRP173069230702
    "pure_numeric": 0.2,          # 022011173948
}
