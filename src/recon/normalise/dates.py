"""
Business-day calendar for settlement timing.

Used both by the generator (to place settlements at a realistic T+2) and,
later, by the matching engine's date-window tolerance in Pass 4. Kept as one
shared module so the two never quietly disagree about what a "business day"
is — a mismatch there would silently bias match rates without ever raising
an error.
"""

from __future__ import annotations

from datetime import date, timedelta

# A deliberately small, hardcoded set of Indian bank holidays for the
# synthetic month(s) this project uses. Not a general-purpose calendar —
# extend this list if you generate data outside these months.
INDIAN_BANK_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi (illustrative — regional holidays vary)
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day / May Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
}


def is_business_day(d: date) -> bool:
    """Mon-Fri, excluding the hardcoded holiday set above."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in INDIAN_BANK_HOLIDAYS_2026:
        return False
    return True


def add_business_days(start: date, n: int) -> date:
    """Advance `start` by `n` business days (T+2 settlement timing).

    This is what makes a payment captured on a Friday settle on Tuesday
    rather than Sunday — the exact case that produces a multi-day gap
    between `created_at` and `settled_at` in real settlement data.
    """
    d = start
    remaining = n
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        d += timedelta(days=step)
        if is_business_day(d):
            remaining -= 1
    return d


def next_business_day(d: date) -> date:
    return add_business_days(d, 1)
