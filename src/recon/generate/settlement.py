"""
Generates Source B: the settlement recon report.

Consumes the OrderPlan list from ledger.py so that refunds, on-hold
payments, and chargebacks in the settlement report are consistent with what
the ledger says happened to each order — rather than the two sources being
generated independently and only coincidentally agreeing, which would make
this a much easier (and less honest) matching problem than the real one.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from recon.generate.anomalies import AnomalyRates, CARD_ISSUERS, CARD_NETWORKS, UTR_STYLE_WEIGHTS
from recon.generate.ledger import OrderPlan
from recon.money import PaymentMethod as MoneyMethod, compute_fee_and_tax
from recon.models import PaymentMethod, SettlementEntityType, SettlementLine, SettlementSummary
from recon.normalise.dates import add_business_days

_ID_ALPHABET = string.ascii_letters + string.digits


def _rand_id(rng: random.Random, n: int = 14) -> str:
    return "".join(rng.choices(_ID_ALPHABET, k=n))


def _to_unix(d: date) -> int:
    # Noon UTC for every synthetic timestamp — arbitrary but consistent,
    # which is all that matters for a matching engine working off deltas.
    return int(datetime.combine(d, time(12, 0), tzinfo=timezone.utc).timestamp())


def _generate_utr(rng: random.Random) -> str:
    styles, weights = zip(*UTR_STYLE_WEIGHTS.items())
    style = rng.choices(styles, weights=weights, k=1)[0]
    if style == "digits_then_letters":
        digits = "".join(rng.choices(string.digits, k=10))
        tail = "".join(rng.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{digits}{tail}"
    if style == "rzrp_prefixed":
        return "RZRP" + "".join(rng.choices(string.digits, k=12))
    return "".join(rng.choices(string.digits, k=12))  # pure_numeric


def _card_fields(method: PaymentMethod | None, rng: random.Random) -> dict:
    if method not in (PaymentMethod.CARD_DEBIT, PaymentMethod.CARD_CREDIT, PaymentMethod.CARD_AMEX):
        return {"card_network": None, "card_issuer": None, "card_type": None}
    network = "Amex" if method == PaymentMethod.CARD_AMEX else rng.choice(
        [n for n in CARD_NETWORKS if n != "Amex"]
    )
    return {
        "card_network": network,
        "card_issuer": rng.choice(CARD_ISSUERS),
        "card_type": "credit" if method == PaymentMethod.CARD_CREDIT else (
            "credit" if method == PaymentMethod.CARD_AMEX else "debit"
        ),
    }


@dataclass
class SettlementGenResult:
    lines: list[SettlementLine]              # all SETTLED lines, batch info attached
    unsettled_lines: list[SettlementLine]    # on-hold payments — never settle, never in a batch
    summaries: list[SettlementSummary]
    duplicate_amount_pairs: list[tuple[str, str]]   # seeded anomaly #12
    order_id_map: dict[str, str] = field(default_factory=dict)      # internal_order_id -> order_...
    payment_entity_map: dict[str, str] = field(default_factory=dict)  # internal_order_id -> pay_...


def generate_settlements(
    rng: random.Random,
    plans: list[OrderPlan],
    rates: AnomalyRates,
) -> SettlementGenResult:
    order_id_map: dict[str, str] = {}
    payment_entity_map: dict[str, str] = {}
    by_settle_date: dict[date, list[SettlementLine]] = {}
    unsettled_lines: list[SettlementLine] = []

    for plan in plans:
        led = plan.ledger
        if led.status.value == "abandoned":
            continue  # nothing enters the settlement report at all

        order_id = f"order_{_rand_id(rng)}"
        order_id_map[led.internal_order_id] = order_id
        payment_entity_id = f"pay_{_rand_id(rng)}"
        payment_entity_map[led.internal_order_id] = payment_entity_id

        money_method = MoneyMethod(led.payment_method.value)
        breakdown = compute_fee_and_tax(led.gross_amount_paise, money_method)
        card_fields = _card_fields(led.payment_method, rng)

        payment_line = SettlementLine(
            entity_id=payment_entity_id,
            type=SettlementEntityType.PAYMENT,
            debit=0,
            credit=breakdown.credit_paise,
            amount=breakdown.gross_paise,
            fee=breakdown.fee_paise,
            tax=breakdown.tax_paise,
            on_hold=plan.is_on_hold,
            settled=not plan.is_on_hold,
            created_at=_to_unix(led.order_date),
            order_id=order_id,
            order_receipt=led.order_receipt,
            method=led.payment_method,
            **card_fields,
            anomaly_tags=list(plan.anomaly_tags),
        )

        if plan.is_on_hold:
            # Captured, but deliberately withheld. Never settles, never
            # appears in a bank statement. The matching engine's job later
            # is to CLASSIFY this correctly, not to raise it as an exception
            # — there's genuinely nothing to match it against yet.
            unsettled_lines.append(payment_line)
            continue

        settled_date = add_business_days(led.order_date, 2)
        by_settle_date.setdefault(settled_date, []).append(payment_line)

        # --- refunds, same-cycle or cross-cycle ---
        if plan.refund_type in ("full_same_cycle", "partial"):
            refund_amount = (
                breakdown.gross_paise
                if plan.refund_type == "full_same_cycle"
                else rng.randint(3, 7) * breakdown.gross_paise // 10  # 30-70%
            )
            refund_line = SettlementLine(
                entity_id=f"rfnd_{_rand_id(rng)}",
                type=SettlementEntityType.REFUND,
                debit=refund_amount,
                credit=0,
                amount=refund_amount,
                fee=0,
                tax=0,
                created_at=_to_unix(led.order_date) + rng.randint(0, 86_400),
                payment_id=payment_entity_id,
                order_id=order_id,
                order_receipt=led.order_receipt,
                method=led.payment_method,
                **card_fields,
                anomaly_tags=list(plan.anomaly_tags),
            )
            # Deliberately netted into the SAME batch as the original
            # payment — this is what makes it a same-cycle case rather than
            # a coincidence. See module docstring.
            by_settle_date[settled_date].append(refund_line)

        elif plan.refund_type == "cross_cycle":
            refund_created = date.fromordinal(
                led.order_date.toordinal() + rng.randint(15, 25)
            )
            refund_settled = add_business_days(refund_created, 2)
            refund_line = SettlementLine(
                entity_id=f"rfnd_{_rand_id(rng)}",
                type=SettlementEntityType.REFUND,
                debit=breakdown.gross_paise,
                credit=0,
                amount=breakdown.gross_paise,
                fee=0,
                tax=0,
                created_at=_to_unix(refund_created),
                payment_id=payment_entity_id,   # the ONLY reliable link back
                order_id=order_id,
                order_receipt=led.order_receipt,
                method=led.payment_method,
                **card_fields,
                anomaly_tags=list(plan.anomaly_tags),
            )
            # Lands weeks later, in a completely different batch — a matcher
            # that follows date-proximity instead of payment_id will fail
            # exactly here.
            by_settle_date.setdefault(refund_settled, []).append(refund_line)

        # --- chargeback / dispute adjustment ---
        if plan.is_chargeback:
            chargeback_date = date.fromordinal(
                led.order_date.toordinal() + rng.randint(20, 40)
            )
            adj_line = SettlementLine(
                entity_id=f"adj_{_rand_id(rng)}",
                type=SettlementEntityType.ADJUSTMENT,
                debit=breakdown.gross_paise,
                credit=0,
                amount=breakdown.gross_paise,
                fee=0,
                tax=0,
                created_at=_to_unix(chargeback_date),
                description="Chargeback debit",
                payment_id=payment_entity_id,
                order_id=order_id,
                order_receipt=led.order_receipt,
                settlement_utr=None,  # adjustments carry a null UTR even once
                                      # batched — matches Razorpay's own
                                      # documented example response
                dispute_id=f"disp_{_rand_id(rng, 10)}",
                anomaly_tags=list(plan.anomaly_tags),
            )
            by_settle_date.setdefault(chargeback_date, []).append(adj_line)

    # --- Route transfers: vendor payouts with no order behind them at all ---
    for settle_date in list(by_settle_date.keys()):
        if rng.random() < rates.p_route_transfer_per_batch:
            transfer_amount = rng.randint(5_000_00, 50_000_00)  # ₹5,000-₹50,000
            fee = transfer_amount * 3 // 1000
            tax = fee * 18 // 100
            by_settle_date[settle_date].append(SettlementLine(
                entity_id=f"trf_{_rand_id(rng)}",
                type=SettlementEntityType.TRANSFER,
                debit=transfer_amount + fee + tax,
                credit=0,
                amount=transfer_amount,
                fee=fee,
                tax=tax,
                created_at=_to_unix(settle_date),
                description="Route transfer to linked account",
                anomaly_tags=["seeded_09_route_transfer"],
            ))

    # --- Assemble settlement summaries, one per settle date, and stamp
    #     settlement_id / settlement_utr / settled_at onto every line ---
    summaries: list[SettlementSummary] = []
    finalized_lines: list[SettlementLine] = []

    for settle_date, lines in sorted(by_settle_date.items()):
        settlement_id = f"setl_{_rand_id(rng)}"
        utr = _generate_utr(rng)
        settled_at = _to_unix(settle_date)
        total_fees = sum(l.fee for l in lines)
        total_tax = sum(l.tax for l in lines)
        net_amount = sum(l.credit for l in lines) - sum(l.debit for l in lines)

        summaries.append(SettlementSummary(
            id=settlement_id, amount=net_amount, fees=total_fees,
            tax=total_tax, utr=utr, created_at=settled_at,
        ))

        for line in lines:
            stamped = line.model_copy(update={
                "settlement_id": settlement_id,
                "settled_at": settled_at,
                # Adjustments keep settlement_utr=None even inside a real
                # batch — everything else inherits the batch UTR.
                "settlement_utr": (
                    None if line.type == SettlementEntityType.ADJUSTMENT else utr
                ),
            })
            finalized_lines.append(stamped)

    duplicate_pairs = _inject_duplicate_amount_collisions(
        summaries, finalized_lines, rng, rates.n_duplicate_amount_pairs_per_month,
    )

    return SettlementGenResult(
        lines=finalized_lines,
        unsettled_lines=unsettled_lines,
        summaries=summaries,
        duplicate_amount_pairs=duplicate_pairs,
        order_id_map=order_id_map,
        payment_entity_map=payment_entity_map,
    )


def _inject_duplicate_amount_collisions(
    summaries: list[SettlementSummary],
    lines: list[SettlementLine],
    rng: random.Random,
    n_pairs: int,
) -> list[tuple[str, str]]:
    """Seeded anomaly #12 — the hardest case in the dataset.

    Manufactures a second, wholly synthetic settlement batch on the same day
    as an existing one, with the EXACT same net amount but a different
    UTR. From the bank's side (amount + date + a possibly-mangled UTR) this
    is genuinely ambiguous; only an intact UTR — or, failing that, careful
    reasoning about batch composition — can disambiguate it. This is the
    case the roadmap says to build the whole demo video around.

    Only SMALL real-world batches are eligible as the "original" side of a
    collision — "small" defined relative to this dataset's own batch-size
    distribution (the bottom quartile by line count), not a fixed constant.
    Two earlier versions of this got it wrong in opposite directions: no
    size limit at all let one run sweep a 102-line and a 29-line batch into
    "bank attribution ambiguous" (133 of 962 records — 14% — flagged by an
    anomaly meant to represent two rare collisions a month); a fixed cutoff
    of 4 lines then overcorrected to zero, because this dataset's batches
    average roughly 27 lines each, so almost nothing was ever small enough.
    A percentile-based cutoff scales with whatever batch sizes this
    particular run actually produces, so it neither disappears nor
    dominates. See FAILURE_LOG.md.

    The synthetic batch's single backing line is a plain adjustment, so its
    own internal netting proof (Pass 3) is trivially self-consistent — the
    ambiguity is entirely at the settlement<->bank layer, not a broken
    arithmetic identity.
    """
    lines_per_settlement: dict[str, int] = {}
    for line in lines:
        if line.settlement_id is not None:
            lines_per_settlement[line.settlement_id] = (
                lines_per_settlement.get(line.settlement_id, 0) + 1
            )

    positive_ids = [s.id for s in summaries if s.amount > 0]
    if not positive_ids or n_pairs <= 0:
        return []

    batch_sizes = sorted(lines_per_settlement.get(sid, 0) for sid in positive_ids)
    # Bottom quartile of THIS run's own batch sizes — always has candidates
    # by construction, regardless of how many orders or batches this
    # particular seed happened to produce.
    size_cutoff = batch_sizes[max(0, len(batch_sizes) // 4)]

    positive_summaries = [
        s for s in summaries
        if s.id in positive_ids and lines_per_settlement.get(s.id, 0) <= size_cutoff
    ]
    if not positive_summaries:
        return []

    # Take the n_pairs SMALLEST eligible batches directly. An earlier
    # version tried to add variety by shuffling a "smallest few" pool sized
    # at n_pairs*3 — but when the quartile-filtered candidate pool itself
    # has only a handful of members (as it does here: 6 candidates for
    # n_pairs=2, i.e. pool size == candidate count), that shuffle draws
    # uniformly from the WHOLE eligible set, silently undoing the smallest-
    # first sort and picking large-within-quartile batches just as often as
    # small ones. Determinism here isn't a loss: for a FIXED seed the result
    # is fixed either way, and minimizing blast radius matters more than
    # variety across runs of the same seed. See FAILURE_LOG.md.
    positive_summaries.sort(key=lambda s: lines_per_settlement.get(s.id, 0))
    candidates = positive_summaries[:n_pairs]
    pairs: list[tuple[str, str]] = []

    for original in candidates:
        twin_id = f"setl_{_rand_id(rng)}"
        twin_utr = _generate_utr(rng)
        twin_summary = SettlementSummary(
            id=twin_id,
            amount=original.amount,       # <- the collision
            fees=0,
            tax=0,
            utr=twin_utr,
            created_at=original.created_at,   # <- same day
        )
        summaries.append(twin_summary)

        filler_line = SettlementLine(
            entity_id=f"adj_{_rand_id(rng)}",
            type=SettlementEntityType.ADJUSTMENT,
            debit=max(0, -original.amount),
            credit=max(0, original.amount),
            amount=abs(original.amount),
            fee=0, tax=0,
            created_at=original.created_at,
            settled_at=original.created_at,
            settlement_id=twin_id,
            settlement_utr=None,
            description="Synthetic filler — see _inject_duplicate_amount_collisions",
            anomaly_tags=["seeded_12_ambiguous_duplicate_amount"],
        )
        lines.append(filler_line)

        # Deliberately NOT tagging the original batch's real order lines
        # here. The ambiguity is at the settlement<->bank linkage level —
        # individual orders inside that batch are still perfectly
        # resolvable via order_id/payment_id (Pass 2) regardless of which
        # bank credit their batch pairs with. Tagging every constituent
        # order as "anomalous" would overstate the anomaly's blast radius
        # and mislabel orders the matcher should have no trouble with.
        # Ground truth records the pair itself via `pairs`, consumed by
        # ground_truth.py at the settlement/bank level, not the order level.
        pairs.append((original.id, twin_id))

    return pairs
