"""
Pass 2 — settlement line <-> internal ledger order join.

Payment lines join directly on their own order_receipt. Refund and
adjustment lines do NOT trust their own order_receipt field as the primary
signal — they follow payment_id back to the ORIGINAL payment entity first,
and use THAT payment's order_receipt. order_receipt on a refund/adjustment
line is treated only as a defensive fallback if payment_id resolution fails.

Why this matters: in the real Razorpay schema, payment_id is the one field
refunds are documented to reliably carry; order_receipt on a refund is not
guaranteed. Building this pass to lean on payment_id first — rather than
reading order_receipt directly, which our OWN generator happens to populate
consistently — is what makes this pass do real work instead of being a
trivial dict lookup that would break the moment it met less tidy data.
"""

from __future__ import annotations

from recon.models import LedgerRecord, SettlementEntityType, SettlementLine

from .types import OrderLinkMethod, P2Result


def link_to_ledger(lines: list[SettlementLine], ledger: list[LedgerRecord]) -> P2Result:
    receipt_to_internal_id = {rec.order_receipt: rec.internal_order_id for rec in ledger}
    entity_by_id = {line.entity_id: line for line in lines}

    order_link: dict[str, str | None] = {}
    link_method: dict[str, OrderLinkMethod] = {}

    for line in lines:
        if line.type == SettlementEntityType.TRANSFER:
            order_link[line.entity_id] = None
            link_method[line.entity_id] = OrderLinkMethod.NONE_EXPECTED
            continue

        if line.type == SettlementEntityType.PAYMENT:
            internal_id = receipt_to_internal_id.get(line.order_receipt)
            if internal_id is not None:
                order_link[line.entity_id] = internal_id
                link_method[line.entity_id] = OrderLinkMethod.DIRECT_RECEIPT
            else:
                order_link[line.entity_id] = None
                link_method[line.entity_id] = OrderLinkMethod.UNRESOLVED
            continue

        # REFUND or ADJUSTMENT — follow payment_id first.
        original_payment = entity_by_id.get(line.payment_id) if line.payment_id else None
        if original_payment is not None:
            internal_id = receipt_to_internal_id.get(original_payment.order_receipt)
            if internal_id is not None:
                order_link[line.entity_id] = internal_id
                link_method[line.entity_id] = OrderLinkMethod.PAYMENT_ID_CHAIN
                continue

        # Fallback: the line's own order_receipt, only if payment_id
        # resolution didn't work.
        internal_id = receipt_to_internal_id.get(line.order_receipt) if line.order_receipt else None
        if internal_id is not None:
            order_link[line.entity_id] = internal_id
            link_method[line.entity_id] = OrderLinkMethod.DIRECT_RECEIPT
        else:
            order_link[line.entity_id] = None
            link_method[line.entity_id] = OrderLinkMethod.UNRESOLVED

    return P2Result(order_link=order_link, link_method=link_method)
