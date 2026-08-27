"""
Tests for recon.money.

These aren't just "does it run" tests — each one asserts a specific numeric
identity or failure mode that the reconciliation engine will depend on later.
If these break, everything downstream is on sand.
"""

import pytest

from recon.money import (
    FeeBreakdown,
    PaymentMethod,
    assert_is_paise,
    compute_fee_and_tax,
    format_rupees,
    rupees_to_paise,
    sum_paise,
)


class TestAssertIsPaise:
    def test_accepts_int(self):
        assert_is_paise(97640, "x")  # should not raise

    def test_rejects_float(self):
        with pytest.raises(TypeError, match="must be an int"):
            assert_is_paise(976.40, "x")

    def test_rejects_bool(self):
        # bool is technically an int subclass in Python — this is the classic
        # trap. `True` must NOT silently pass as `1` paise.
        with pytest.raises(TypeError, match="bool"):
            assert_is_paise(True, "x")

    def test_rejects_string(self):
        with pytest.raises(TypeError):
            assert_is_paise("97640", "x")


class TestComputeFeeAndTax:
    def test_the_worked_example_from_the_roadmap(self):
        """₹1,000 card payment -> fee ₹20.00, tax ₹3.60, credit ₹976.40.

        This is the exact example walked through by hand in the roadmap.
        If this test ever fails, the bug is almost certainly in the rate
        table or the rounding function, not in this test.
        """
        result = compute_fee_and_tax(100_000, PaymentMethod.CARD_CREDIT)
        assert result.gross_paise == 100_000
        assert result.fee_paise == 2_000       # ₹20.00 (2% MDR)
        assert result.tax_paise == 360         # ₹3.60 (18% of ₹20)
        assert result.credit_paise == 97_640   # ₹976.40

    def test_upi_has_zero_fee_and_zero_tax(self):
        result = compute_fee_and_tax(50_000, PaymentMethod.UPI)
        assert result.fee_paise == 0
        assert result.tax_paise == 0
        assert result.credit_paise == 50_000

    def test_netting_identity_always_holds(self):
        """credit = gross - fee - tax, exactly, for every method and amount.

        This is the arithmetic proof at the heart of Pass 3 of the matching
        engine. It has to be bulletproof here before it's trusted anywhere
        else in the pipeline.
        """
        for method in PaymentMethod:
            for gross in [1, 99, 100_000, 1, 999_999, 48_230_100]:
                result = compute_fee_and_tax(gross, method)
                assert result.credit_paise == result.gross_paise - result.fee_paise - result.tax_paise

    def test_rejects_float_input(self):
        with pytest.raises(TypeError):
            compute_fee_and_tax(1000.50, PaymentMethod.CARD_DEBIT)

    def test_amex_rate_is_higher_than_domestic_debit(self):
        # Guards against the rate table silently collapsing to one flat rate
        amex = compute_fee_and_tax(100_000, PaymentMethod.CARD_AMEX)
        debit = compute_fee_and_tax(100_000, PaymentMethod.CARD_DEBIT)
        assert amex.fee_paise > debit.fee_paise


class TestFeeBreakdownIntegrity:
    def test_cannot_construct_an_inconsistent_breakdown(self):
        """The dataclass itself refuses to hold numbers that don't add up —
        this should be structurally impossible, not just conventionally true."""
        with pytest.raises(AssertionError):
            FeeBreakdown(
                gross_paise=100_000,
                fee_paise=2_000,
                tax_paise=360,
                credit_paise=99_999,  # wrong on purpose
            )


class TestSumPaise:
    def test_sums_correctly(self):
        assert sum_paise([100, 200, 300]) == 600

    def test_empty_list_is_zero(self):
        assert sum_paise([]) == 0

    def test_rejects_a_float_hiding_in_the_list(self):
        # This is the exact bug a stray pandas float column would cause
        with pytest.raises(TypeError):
            sum_paise([100, 200.5, 300])


class TestFormatRupees:
    def test_worked_example(self):
        assert format_rupees(97_640) == "\u20b9976.40"

    def test_indian_digit_grouping(self):
        # ₹4,82,301.00 — note the Indian grouping (2s after the first 3),
        # not the western 4,82,301 style of 482,301
        assert format_rupees(48_230_100) == "\u20b94,82,301.00"

    def test_negative_amount(self):
        assert format_rupees(-1_500) == "-\u20b915.00"

    def test_zero(self):
        assert format_rupees(0) == "\u20b90.00"

    def test_rejects_float(self):
        with pytest.raises(TypeError):
            format_rupees(976.40)


class TestRupeesToPaise:
    def test_parses_plain_string(self):
        assert rupees_to_paise("976.40") == 97_640

    def test_parses_with_commas_and_symbol(self):
        assert rupees_to_paise("\u20b94,82,301.00") == 48_230_100

    def test_round_trip_with_format_rupees(self):
        original = 48_230_100
        assert rupees_to_paise(format_rupees(original)) == original

    def test_parses_negative(self):
        assert rupees_to_paise("-15.00") == -1_500
