"""
Generates Source A: the merchant's internal order ledger.

This module ALSO decides, per order, which downstream anomaly (if any)
applies — refund type, on-hold status, payment method. Those decisions are
returned as `OrderPlan` objects (ledger record + generation-only intent) so
`settlement.py` can build a settlement recon report that's consistent with
what the ledger says happened, rather than the two sources being generated
independently and coincidentally agreeing.
"""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass, field
from datetime import date

from recon.generate.anomalies import AnomalyRates
from recon.models import Channel, LedgerRecord, OrderStatus, PaymentMethod

# Weighted payment-method mix for orders that are NOT forced into UPI/Amex by
# the anomaly rates. Rough approximation of an Indian D2C merchant's mix.
_OTHER_METHOD_WEIGHTS: list[tuple[PaymentMethod, float]] = [
    (PaymentMethod.CARD_DEBIT, 0.35),
    (PaymentMethod.CARD_CREDIT, 0.30),
    (PaymentMethod.NETBANKING, 0.20),
    (PaymentMethod.WALLET, 0.15),
]

_CHANNEL_WEIGHTS: list[tuple[Channel, float]] = [
    (Channel.WEB, 0.55),
    (Channel.APP, 0.35),
    (Channel.PAYMENT_LINK, 0.10),
]

# Order-value bands (paise), each a (low, high, weight) — skewed toward
# smaller baskets, a long thin tail toward larger ones.
_VALUE_BANDS: list[tuple[int, int, float]] = [
    (20_000, 200_000, 0.70),      # ₹200 - ₹2,000
    (200_000, 800_000, 0.25),     # ₹2,000 - ₹8,000
    (800_000, 5_000_000, 0.05),   # ₹8,000 - ₹50,000
]


def _weighted_choice(rng: random.Random, options: list[tuple]):
    items, weights = zip(*options)
    return rng.choices(items, weights=weights, k=1)[0]


def _random_order_value(rng: random.Random) -> int:
    # _VALUE_BANDS is (low, high, weight) — a 3-tuple, so the generic
    # 2-tuple _weighted_choice() doesn't apply here directly.
    weights = [w for _, _, w in _VALUE_BANDS]
    low, high, _ = rng.choices(_VALUE_BANDS, weights=weights, k=1)[0]
    return rng.randrange(low, high, 100)  # keep it a whole number of rupees


def _approx_goods_gst(gross_paise: int) -> int:
    """Approximate GST-on-goods extracted from an MRP-inclusive price.

    This is descriptive metadata for the ledger only — it is NOT part of the
    settlement netting arithmetic (that's MDR + GST-on-MDR, a completely
    different number, computed in money.py). Documented here so nobody
    confuses the two when reading the generated data later.
    """
    return (gross_paise * 18) // 118


@dataclass
class OrderPlan:
    """A ledger record plus the generation-time decisions settlement.py needs
    to produce a consistent settlement recon report for this order."""
    ledger: LedgerRecord
    refund_type: str | None = None      # None | "full_same_cycle" | "partial" | "cross_cycle"
    is_on_hold: bool = False
    is_chargeback: bool = False
    anomaly_tags: list[str] = field(default_factory=list)


def generate_ledger(
    rng: random.Random,
    month: str,
    n_orders: int,
    rates: AnomalyRates,
    n_customers: int = 300,
) -> list[OrderPlan]:
    """Generate `n_orders` orders spread across `month` ("YYYY-MM").

    Returns OrderPlan objects, not bare LedgerRecords — settlement.py needs
    the extra intent fields to stay consistent with this ledger.
    """
    year, mon = (int(x) for x in month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    customer_pool = [f"cust_{i:05d}" for i in range(n_customers)]

    plans: list[OrderPlan] = []

    for i in range(1, n_orders + 1):
        internal_order_id = f"ORD-{year}{mon:02d}-{i:06d}"
        order_receipt = f"rcpt_{year}{mon:02d}{i:06d}"
        customer_id = rng.choice(customer_pool)
        order_date = date(year, mon, rng.randint(1, days_in_month))
        gross = _random_order_value(rng)
        goods_gst = _approx_goods_gst(gross)
        channel = _weighted_choice(rng, _CHANNEL_WEIGHTS)
        sku_count = rng.choices([1, 2, 3, 4, 5], weights=[45, 25, 15, 10, 5])[0]

        # --- decide the fate of this order ---
        anomaly_tags: list[str] = []
        roll = rng.random()

        if roll < rates.p_abandoned:
            plans.append(OrderPlan(
                ledger=LedgerRecord(
                    internal_order_id=internal_order_id,
                    order_receipt=order_receipt,
                    customer_id=customer_id,
                    order_date=order_date,
                    gross_amount_paise=gross,
                    goods_gst_paise=goods_gst,
                    status=OrderStatus.ABANDONED,
                    channel=channel,
                    sku_count=sku_count,
                    payment_method=None,
                ),
                anomaly_tags=["seeded_14_abandoned"],
            ))
            continue

        # payment method — anomaly rates get first refusal on UPI/Amex so the
        # overall mix matches the configured rates rather than drifting
        method_roll = rng.random()
        if method_roll < rates.p_upi:
            method = PaymentMethod.UPI
            anomaly_tags.append("seeded_02_upi_zero_fee")
        elif method_roll < rates.p_upi + rates.p_amex:
            method = PaymentMethod.CARD_AMEX
            anomaly_tags.append("seeded_16_amex_fee_variance")
        else:
            method = _weighted_choice(rng, _OTHER_METHOD_WEIGHTS)

        # on-hold / chargeback / refund are mutually exclusive per order —
        # each represents a different reason the money never simply "just
        # settles", and combining them would be an unrealistic double anomaly
        refund_type: str | None = None
        is_on_hold = False
        is_chargeback = False
        status = OrderStatus.PAID

        outcome_roll = rng.random()
        cum = 0.0
        if outcome_roll < (cum := cum + rates.p_on_hold):
            is_on_hold = True
            status = OrderStatus.ON_HOLD
            anomaly_tags.append("seeded_06_on_hold")
        elif outcome_roll < (cum := cum + rates.p_chargeback_adjustment):
            is_chargeback = True
            anomaly_tags.append("seeded_07_chargeback_adjustment")
        elif outcome_roll < (cum := cum + rates.p_cross_cycle_refund):
            refund_type = "cross_cycle"
            status = OrderStatus.REFUNDED
            anomaly_tags.append("seeded_05_cross_cycle_refund")
        elif outcome_roll < (cum := cum + rates.p_full_refund_same_cycle):
            refund_type = "full_same_cycle"
            status = OrderStatus.REFUNDED
            anomaly_tags.append("seeded_03_full_refund_same_cycle")
        elif outcome_roll < (cum := cum + rates.p_partial_refund):
            refund_type = "partial"
            status = OrderStatus.PARTIALLY_REFUNDED
            anomaly_tags.append("seeded_04_partial_refund")

        plans.append(OrderPlan(
            ledger=LedgerRecord(
                internal_order_id=internal_order_id,
                order_receipt=order_receipt,
                customer_id=customer_id,
                order_date=order_date,
                gross_amount_paise=gross,
                goods_gst_paise=goods_gst,
                status=status,
                channel=channel,
                sku_count=sku_count,
                payment_method=method,
            ),
            refund_type=refund_type,
            is_on_hold=is_on_hold,
            is_chargeback=is_chargeback,
            anomaly_tags=anomaly_tags,
        ))

    return plans
