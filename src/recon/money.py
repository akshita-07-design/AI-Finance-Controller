"""
Money arithmetic for the reconciliation engine.

THE ONE RULE OF THIS ENTIRE PROJECT:
    Every rupee amount, everywhere in this codebase, is an `int` number of paise.
    Never a float. Never a Decimal, unless you have a specific reason and document it.

Why: binary floating point cannot represent most decimal fractions exactly
(0.1 + 0.2 != 0.3 in every mainstream language). Across a few thousand
transactions that drifts into silent, untraceable variance — exactly the kind
of bug a finance reconciliation tool cannot afford to have baked into its own
arithmetic. Integers in the smallest currency unit (paise) are exact for
addition, subtraction, and comparison. Convert to rupees only at display time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


Paise = int  # type alias for readability at call sites — still just an int


class PaymentMethod(str, Enum):
    UPI = "upi"
    NETBANKING = "netbanking"
    CARD_DEBIT = "card_debit"
    CARD_CREDIT = "card_credit"
    CARD_AMEX = "card_amex"
    WALLET = "wallet"


# MDR rate table, expressed in basis points of a basis point (i.e. hundredths
# of a percent, scaled by 100) so the whole computation stays integer:
#   200 -> 2.00%    100 -> 1.00%    0 -> 0.00%
# These are plausible-realistic rates for a synthetic dataset, not authoritative
# Razorpay pricing — say so explicitly in the README, don't imply precision you
# don't have.
MDR_BPS: dict[PaymentMethod, int] = {
    PaymentMethod.UPI: 0,
    PaymentMethod.NETBANKING: 190,
    PaymentMethod.CARD_DEBIT: 100,
    PaymentMethod.CARD_CREDIT: 200,
    PaymentMethod.CARD_AMEX: 300,
    PaymentMethod.WALLET: 200,
}

GST_ON_MDR_BPS = 1800  # 18%, applied to the fee, not to the payment amount


def assert_is_paise(value, name: str = "value") -> None:
    """Fail loudly the moment a float sneaks into a money-typed field.

    Call this at the boundary of any function that accepts an amount — the
    generator, the parsers, the matcher. Cheap insurance against the single
    most common bug class in this kind of project.
    """
    if isinstance(value, bool):
        raise TypeError(f"{name} is a bool, not an int amount of paise: {value!r}")
    if not isinstance(value, int):
        raise TypeError(
            f"{name} must be an int (paise), got {type(value).__name__}: {value!r}. "
            "Money is never a float in this codebase — see money.py docstring."
        )


def _round_half_even(numerator: int, denominator: int) -> int:
    """Integer division with round-half-to-even ("banker's rounding").

    Used for fee/tax computation so remainders don't systematically drift in
    one direction across thousands of transactions. Python's own `round()`
    already rounds half-to-even for floats, but we do this in pure integer
    arithmetic so no float ever touches a money value, even transiently.
    """
    quotient, remainder = divmod(numerator, denominator)
    twice_remainder = 2 * remainder
    if twice_remainder > denominator:
        quotient += 1
    elif twice_remainder == denominator and quotient % 2 == 1:
        quotient += 1
    return quotient


@dataclass(frozen=True)
class FeeBreakdown:
    gross_paise: Paise
    fee_paise: Paise       # MDR
    tax_paise: Paise       # GST on the MDR
    credit_paise: Paise    # what the merchant actually nets

    def __post_init__(self):
        assert_is_paise(self.gross_paise, "gross_paise")
        assert_is_paise(self.fee_paise, "fee_paise")
        assert_is_paise(self.tax_paise, "tax_paise")
        assert_is_paise(self.credit_paise, "credit_paise")
        # Internal consistency: this identity must hold by construction, not
        # by luck. If it doesn't, something upstream is already broken.
        assert self.credit_paise == self.gross_paise - self.fee_paise - self.tax_paise, (
            "FeeBreakdown failed its own arithmetic identity — this should be "
            "structurally impossible; check compute_fee_and_tax()"
        )


def compute_fee_and_tax(gross_paise: Paise, method: PaymentMethod) -> FeeBreakdown:
    """The core netting calculation: gross payment -> what actually settles.

    fee = gross * MDR_rate                 (rounded, banker's rounding)
    tax = fee * 18% GST                    (rounded, banker's rounding)
    credit = gross - fee - tax             (exact, by subtraction)
    """
    assert_is_paise(gross_paise, "gross_paise")

    mdr_bps = MDR_BPS[method]
    fee_paise = _round_half_even(gross_paise * mdr_bps, 10_000)
    tax_paise = _round_half_even(fee_paise * GST_ON_MDR_BPS, 10_000)
    credit_paise = gross_paise - fee_paise - tax_paise

    return FeeBreakdown(
        gross_paise=gross_paise,
        fee_paise=fee_paise,
        tax_paise=tax_paise,
        credit_paise=credit_paise,
    )


def sum_paise(amounts) -> Paise:
    """Sum a list of paise amounts, asserting each is a real int as it goes.

    Prefer this over bare `sum(...)` at any point where the list might
    contain something that leaked in from outside the pipeline (e.g. straight
    off a pandas column, where an accidental float column is an easy mistake).
    """
    total = 0
    for i, amount in enumerate(amounts):
        assert_is_paise(amount, f"amounts[{i}]")
        total += amount
    return total


def format_rupees(paise: Paise) -> str:
    """Paise -> display string. The ONLY place a decimal point should appear."""
    assert_is_paise(paise, "paise")
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, remainder = divmod(paise, 100)
    # Indian digit grouping: last 3 digits, then groups of 2 (e.g. 12,34,567)
    rupee_str = str(rupees)
    if len(rupee_str) > 3:
        last3 = rupee_str[-3:]
        rest = rupee_str[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        rupee_str = ",".join(groups) + "," + last3
    return f"{sign}\u20b9{rupee_str}.{remainder:02d}"


def rupees_to_paise(rupees_str: str) -> Paise:
    """Parse a human-entered rupee string (e.g. "1,000.50" or "1000.5") to paise.

    Use this ONLY at the input boundary (reading a config, a CLI flag, a raw
    CSV cell that a human typed). Never use it inside the engine itself —
    by the time data is inside the pipeline it should already be int paise.
    """
    cleaned = rupees_str.replace(",", "").replace("\u20b9", "").strip()
    if "." in cleaned:
        whole, frac = cleaned.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = cleaned, "00"
    whole = whole or "0"
    sign = -1 if whole.startswith("-") else 1
    whole = whole.lstrip("-")
    return sign * (int(whole) * 100 + int(frac))
