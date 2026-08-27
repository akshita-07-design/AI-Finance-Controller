"""
Unit tests for the matching engine (Passes 1-4), using small hand-built
fixtures rather than the full generator — faster, and each test targets one
specific behavior directly, including regression tests for bugs caught
while building this against real generated data (see FAILURE_LOG.md).
"""

from __future__ import annotations

from datetime import date

import pytest

from recon.match.engine import run_matching
from recon.match.p1_exact import match_exact_utr
from recon.match.p2_ledger import link_to_ledger
from recon.match.p3_arithmetic import prove_batches
from recon.match.p4_amount_date import resolve_amount_and_date
from recon.match.p4_fuzzy import resolve_fuzzy
from recon.match.types import OrderLinkMethod, VarianceClass
from recon.models import (
    BankRow,
    Channel,
    LedgerRecord,
    OrderStatus,
    PaymentMethod,
    SettlementEntityType,
    SettlementLine,
    SettlementSummary,
)


def make_summary(id_, amount, utr, created_at=1_775_000_000, fees=0, tax=0):
    return SettlementSummary(id=id_, amount=amount, fees=fees, tax=tax, utr=utr, created_at=created_at)


def make_bank_row(bank_txn_id, txn_date, narration, credit=0, debit=0, ref_no=None, value_date=None):
    return BankRow(
        bank_txn_id=bank_txn_id, txn_date=txn_date, value_date=value_date or txn_date,
        narration=narration, ref_no=ref_no, debit_paise=debit, credit_paise=credit,
        balance_paise=10_000_000,
    )


def make_payment_line(entity_id, amount, credit, settlement_id, utr, order_id="order_x",
                       order_receipt="rcpt_1", fee=0, tax=0):
    return SettlementLine(
        entity_id=entity_id, type=SettlementEntityType.PAYMENT, debit=0, credit=credit,
        amount=amount, fee=fee, tax=tax, created_at=1_775_000_000, settled_at=1_775_000_000,
        settlement_id=settlement_id, settlement_utr=utr, order_id=order_id,
        order_receipt=order_receipt, method=PaymentMethod.CARD_DEBIT,
    )


def make_ledger(internal_order_id="ORD-1", order_receipt="rcpt_1"):
    return LedgerRecord(
        internal_order_id=internal_order_id, order_receipt=order_receipt, customer_id="cust_1",
        order_date=date(2026, 4, 1), gross_amount_paise=100_000, goods_gst_paise=15_000,
        status=OrderStatus.PAID, channel=Channel.WEB, sku_count=1,
        payment_method=PaymentMethod.CARD_DEBIT,
    )


class TestP1ExactUTR:
    def test_clean_exact_match(self):
        summaries = [make_summary("setl_A", 97_640, "1568176960vxp0rj")]
        bank_rows = [make_bank_row("BNK-1", date(2026, 4, 3),
                                    "1568176960vxp0rj RAZORPAY SETTLEMENT", credit=97_640)]
        result = match_exact_utr(summaries, bank_rows)
        assert result.matched == {"setl_A": "BNK-1"}
        assert result.unresolved_settlement_ids == []
        assert result.ambiguous == []

    def test_reversal_debit_does_not_create_false_ambiguity(self):
        """Regression test: a reversal debit sharing the same UTR/ref_no as
        a settlement's genuine credit must NOT be treated as a competing
        candidate — Pass 1 originally lumped both directions together,
        producing a false 3-way ambiguous group. See FAILURE_LOG.md."""
        summaries = [make_summary("setl_A", 50_000, "RZRP111111111111")]
        bank_rows = [
            make_bank_row("BNK-1", date(2026, 4, 3), "RZRP111111111111 SETTLEMENT", credit=50_000),
            make_bank_row("BNK-2", date(2026, 4, 4), "REVERSAL", debit=50_000,
                           ref_no="RZRP111111111111"),
        ]
        result = match_exact_utr(summaries, bank_rows)
        assert result.matched == {"setl_A": "BNK-1"}
        assert result.ambiguous == []
        # BNK-2 legitimately stays unresolved (nothing else claims a
        # standalone reversal debit) — the important assertion is that it
        # did NOT contaminate setl_A's clean match above.

    def test_duplicate_same_sign_credit_resolves_to_earliest_and_flags_rest(self):
        """A genuine duplicate credit (seeded anomaly #13): same UTR, same
        direction, two bank rows. The settlement should still resolve
        (to the earliest row), with the extra flagged separately — not left
        as an unresolved ambiguity."""
        summaries = [make_summary("setl_A", 50_000, "RZRP222222222222")]
        bank_rows = [
            make_bank_row("BNK-1", date(2026, 4, 3), "RZRP222222222222 SETTLEMENT", credit=50_000),
            make_bank_row("BNK-2", date(2026, 4, 3), "RZRP222222222222 SETTLEMENT", credit=50_000),
        ]
        result = match_exact_utr(summaries, bank_rows)
        assert result.matched == {"setl_A": "BNK-1"}
        assert result.duplicate_same_sign == {"setl_A": ["BNK-2"]}
        assert result.ambiguous == []

    def test_genuine_cross_settlement_ambiguity_is_flagged_not_guessed(self):
        """Two DIFFERENT settlements' UTRs both appearing as candidates on
        the same bank row — real ambiguity, must defer."""
        summaries = [
            make_summary("setl_A", 50_000, "RZRP333333333333"),
            make_summary("setl_B", 50_000, "RZRP333333333334"),  # deliberately similar-looking
        ]
        # Contrived: a single narration that happens to contain both exact
        # UTR substrings (rare in practice, but the pass must handle it).
        bank_rows = [make_bank_row(
            "BNK-1", date(2026, 4, 3),
            "RZRP333333333333 RZRP333333333334", credit=50_000,
        )]
        result = match_exact_utr(summaries, bank_rows)
        assert result.matched == {}
        assert len(result.ambiguous) == 1
        assert set(result.ambiguous[0].settlement_ids) == {"setl_A", "setl_B"}


class TestP4Fuzzy:
    def test_mangled_utr_recovered_when_amount_agrees(self):
        summaries = [make_summary("setl_A", 50_000, "1568176960vxp0rj")]
        bank_rows = [make_bank_row(
            "BNK-1", date(2026, 4, 3), "156817696Ovxp0rj SETTLEMENT",  # O instead of 0
            credit=50_000,
        )]
        from recon.match.p1_exact import match_exact_utr
        p1 = match_exact_utr(summaries, bank_rows)
        assert p1.matched == {}  # exact match correctly fails first

        p4 = resolve_fuzzy(p1, summaries, bank_rows)
        assert p4.matched == {"setl_A": "BNK-1"}

    def test_mangled_utr_rejected_when_amount_disagrees(self):
        """Two independent signals required — UTR closeness alone must not
        be enough (roadmap Part 5.5: one signal is a guess, two agreeing is
        evidence)."""
        summaries = [make_summary("setl_A", 50_000, "1568176960vxp0rj")]
        bank_rows = [make_bank_row(
            "BNK-1", date(2026, 4, 3), "156817696Ovxp0rj SETTLEMENT",
            credit=99_999,  # wrong amount
        )]
        from recon.match.p1_exact import match_exact_utr
        p1 = match_exact_utr(summaries, bank_rows)
        p4 = resolve_fuzzy(p1, summaries, bank_rows)
        assert p4.matched == {}
        assert "setl_A" in p4.still_unresolved_settlement_ids

    def test_unrelated_utrs_are_not_confused_with_each_other(self):
        """Regression test: two settlements with completely independent
        (not confusion-related) UTRs and the SAME amount must not be fuzzy-
        matched to each other just because amount agrees — canonical edit
        distance must also be close. Caught during real data testing when
        two unrelated UTRs were 12 edits apart yet both had matching
        amounts; the amount check alone would have falsely reconciled them
        with the wrong UTR. See FAILURE_LOG.md."""
        summaries = [make_summary("setl_A", 50_000, "1568176960vxp0rj")]
        bank_rows = [make_bank_row(
            "BNK-1", date(2026, 4, 3), "2779964579f874es SETTLEMENT",  # unrelated UTR, same amount
            credit=50_000,
        )]
        from recon.match.p1_exact import match_exact_utr
        p1 = match_exact_utr(summaries, bank_rows)
        p4 = resolve_fuzzy(p1, summaries, bank_rows)
        assert p4.matched == {}, "should not match on amount alone when UTR is unrelated"


class TestP4AmountDate:
    def test_unique_amount_date_pair_with_no_utr_signal_resolves(self):
        summaries = [make_summary("setl_A", 50_000, "1568176960vxp0rj", created_at=1_775_000_000)]
        bank_rows = [make_bank_row(
            "BNK-1", date(2026, 3, 31), "NEFT CR: HDFC RAZORPAY SETTLEMENT",  # no UTR at all
            credit=50_000,
        )]
        from recon.match.p1_exact import match_exact_utr
        p1 = match_exact_utr(summaries, bank_rows)
        fuzzy = resolve_fuzzy(p1, summaries, bank_rows)
        p4 = resolve_amount_and_date(p1, fuzzy, summaries, bank_rows)
        assert p4.matched == {"setl_A": "BNK-1"}

    def test_two_settlements_same_amount_same_date_no_utr_is_genuinely_ambiguous(self):
        """The flagship anomaly, in miniature: with no UTR signal at all on
        either side, two settlements sharing amount+date cannot be told
        apart, and the correct behavior is to decline, not guess."""
        summaries = [
            make_summary("setl_A", 50_000, "utrAAAAAAAAAAAAAA", created_at=1_775_000_000),
            make_summary("setl_B", 50_000, "utrBBBBBBBBBBBBBB", created_at=1_775_000_000),
        ]
        bank_rows = [
            make_bank_row("BNK-1", date(2026, 3, 31), "NEFT CR: HDFC SETTLEMENT", credit=50_000),
            make_bank_row("BNK-2", date(2026, 3, 31), "IMPS/RZPY/SETTLEMENT", credit=50_000),
        ]
        from recon.match.p1_exact import match_exact_utr
        p1 = match_exact_utr(summaries, bank_rows)
        fuzzy = resolve_fuzzy(p1, summaries, bank_rows)
        p4 = resolve_amount_and_date(p1, fuzzy, summaries, bank_rows)
        assert p4.matched == {}
        assert len(p4.ambiguous) == 1
        assert set(p4.ambiguous[0].settlement_ids) == {"setl_A", "setl_B"}
        assert set(p4.ambiguous[0].bank_txn_ids) == {"BNK-1", "BNK-2"}


class TestP2Ledger:
    def test_payment_resolves_via_direct_receipt(self):
        ledger = [make_ledger("ORD-1", "rcpt_1")]
        lines = [make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1",
                                    order_receipt="rcpt_1")]
        result = link_to_ledger(lines, ledger)
        assert result.order_link["pay_1"] == "ORD-1"
        assert result.link_method["pay_1"] == OrderLinkMethod.DIRECT_RECEIPT

    def test_refund_resolves_via_payment_id_chain_even_without_its_own_receipt(self):
        """The real test of Pass 2: a refund line whose OWN order_receipt is
        missing must still resolve correctly by following payment_id back
        to the original payment. If Pass 2 were just a naive dict lookup on
        each line's own order_receipt, this would fail."""
        ledger = [make_ledger("ORD-1", "rcpt_1")]
        payment = make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1",
                                     order_receipt="rcpt_1")
        refund = SettlementLine(
            entity_id="rfnd_1", type=SettlementEntityType.REFUND, debit=100_000, credit=0,
            amount=100_000, created_at=1_775_100_000, settlement_id="setl_B",
            settlement_utr="utr2", payment_id="pay_1",
            order_receipt=None,  # deliberately absent
        )
        result = link_to_ledger([payment, refund], ledger)
        assert result.order_link["rfnd_1"] == "ORD-1"
        assert result.link_method["rfnd_1"] == OrderLinkMethod.PAYMENT_ID_CHAIN

    def test_transfer_has_no_order_expected(self):
        transfer = SettlementLine(
            entity_id="trf_1", type=SettlementEntityType.TRANSFER, debit=50_000, credit=0,
            amount=50_000, created_at=1_775_000_000, settlement_id="setl_A", settlement_utr="utr1",
        )
        result = link_to_ledger([transfer], [])
        assert result.order_link["trf_1"] is None
        assert result.link_method["trf_1"] == OrderLinkMethod.NONE_EXPECTED

    def test_unresolvable_line_is_flagged_not_silently_dropped(self):
        payment = make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1",
                                     order_receipt="rcpt_nonexistent")
        result = link_to_ledger([payment], [make_ledger("ORD-1", "rcpt_1")])
        assert result.order_link["pay_1"] is None
        assert result.link_method["pay_1"] == OrderLinkMethod.UNRESOLVED


class TestP3Arithmetic:
    def test_batch_that_nets_correctly_is_ok(self):
        lines = [make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1")]
        summaries = [make_summary("setl_A", 97_640, "utr1")]
        bank_rows = [make_bank_row("BNK-1", date(2026, 4, 3), "utr1", credit=97_640)]
        result = prove_batches(lines, summaries, {"setl_A": "BNK-1"}, bank_rows)
        assert result.batch_variances["setl_A"].variance_class == VarianceClass.OK
        assert result.batch_variances["setl_A"].delta_paise == 0

    def test_rounding_drift_under_a_rupee_is_classified_separately_from_real_variance(self):
        lines = [make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1")]
        summaries = [make_summary("setl_A", 97_640, "utr1")]
        bank_rows = [make_bank_row("BNK-1", date(2026, 4, 3), "utr1", credit=97_641)]  # 1 paise off
        result = prove_batches(lines, summaries, {"setl_A": "BNK-1"}, bank_rows)
        assert result.batch_variances["setl_A"].variance_class == VarianceClass.ROUNDING_DRIFT

    def test_real_variance_is_flagged_unexplained(self):
        lines = [make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1")]
        summaries = [make_summary("setl_A", 97_640, "utr1")]
        bank_rows = [make_bank_row("BNK-1", date(2026, 4, 3), "utr1", credit=90_000)]  # way off
        result = prove_batches(lines, summaries, {"setl_A": "BNK-1"}, bank_rows)
        assert result.batch_variances["setl_A"].variance_class == VarianceClass.UNEXPLAINED
        assert result.batch_variances["setl_A"].delta_paise == -7_640

    def test_ambiguous_pairing_does_not_break_arithmetic_proof(self):
        """A batch that hasn't been matched to any bank row yet (e.g. it's
        one side of an ambiguous pair) should still be checked against its
        OWN summary claim — arithmetic proof and bank-attribution ambiguity
        are independent questions (see engine.py docstring)."""
        lines = [make_payment_line("pay_1", 100_000, 97_640, "setl_A", "utr1")]
        summaries = [make_summary("setl_A", 97_640, "utr1")]
        result = prove_batches(lines, summaries, {}, [])  # no bank match at all
        assert result.batch_variances["setl_A"].variance_class == VarianceClass.OK
        assert result.batch_variances["setl_A"].bank_net is None


class TestEngineClassification:
    def test_a_settlement_unresolved_to_any_bank_row_is_not_counted_as_fully_resolved(self, tmp_path):
        """Regression test for a real classification gap: a settlement that
        never matched ANY bank row (not exact, not fuzzy, not amount+date —
        e.g. a UTR truncated mid-string by a real 50-char narration limit,
        leaving a partial candidate too different to fuzzy-match but not
        'zero signal' either, so it never reached the amount+date fallback)
        was NOT flagged ambiguous, and Pass 3 compared it against its own
        summary claim (self-consistent by construction, so variance_ok was
        True) — meaning `fully_resolved` was silently True even though we
        never identified which bank transaction represents its payout.
        Caught by tracing one specific unresolved record in real generated
        data, not by any test that existed before this one. See
        FAILURE_LOG.md."""
        import json

        import pandas as pd

        ledger_df = pd.DataFrame([{
            "internal_order_id": "ORD-1", "order_receipt": "rcpt_1", "customer_id": "cust_1",
            "order_date": "2026-04-01", "gross_amount_paise": 100_000, "goods_gst_paise": 15_000,
            "status": "paid", "channel": "web", "sku_count": 1, "payment_method": "card_debit",
        }])
        ledger_df.to_csv(tmp_path / "internal_ledger.csv", index=False)

        settlement_line = {
            "entity_id": "pay_1", "type": "payment", "debit": 0, "credit": 97_640,
            "amount": 100_000, "currency": "INR", "fee": 2_000, "tax": 360,
            "on_hold": False, "settled": True, "created_at": 1_775_000_000,
            "settled_at": 1_775_100_000, "settlement_id": "setl_A",
            "settlement_utr": "1568176960vxp0rj", "order_id": "order_x",
            "order_receipt": "rcpt_1", "method": "card_debit",
        }
        with open(tmp_path / "settlement_recon.json", "w") as f:
            json.dump({"entity": "collection", "count": 1, "items": [settlement_line]}, f)

        with open(tmp_path / "settlement_summaries.json", "w") as f:
            json.dump([{
                "id": "setl_A", "entity": "settlement", "amount": 97_640, "status": "processed",
                "fees": 2_000, "tax": 360, "utr": "1568176960vxp0rj", "created_at": 1_775_100_000,
            }], f)

        # A narration truncated mid-UTR — carries a partial, unmatchable
        # candidate, deliberately NOT zero-signal (so it must not fall
        # through to the amount+date rescue path either).
        bank_df = pd.DataFrame([{
            "bank_txn_id": "BNK-1", "txn_date": "2026-04-03", "value_date": "2026-04-03",
            "narration": "NEFT CR-HDFC0000001-RAZORPAY SOFTWARE-1568176960vx",  # cut mid-UTR
            "ref_no": None, "debit_paise": 0, "credit_paise": 97_640,
            "balance_paise": 10_000_000, "anomaly_tags": "[]",
        }])
        bank_df.to_csv(tmp_path / "bank_statement.csv", index=False)

        report = run_matching(tmp_path)
        classification = report.classifications["pay_1"]
        assert classification.order_resolved is True   # order-level join is fine
        assert classification.bank_ambiguous is False   # correctly not flagged as ambiguous
        assert classification.fully_resolved is False, (
            "a settlement matched to NO bank row at all must not be counted "
            "as fully resolved just because it also isn't ambiguous"
        )
