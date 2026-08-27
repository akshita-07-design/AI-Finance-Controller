"""
Generates Source C: the bank statement.

Deliberately the messiest source, because that is true of real bank
statements: free-text narrations, occasional value-date skew, unrelated
noise rows the matcher must correctly ignore, and a running balance that
exists purely as a self-consistency check on this generator itself — if the
balance column doesn't foot, nothing downstream should be trusted either.
"""

from __future__ import annotations

import random
import string
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from recon.generate.anomalies import AnomalyRates
from recon.models import BankRow, SettlementSummary
from recon.normalise.dates import next_business_day

_UTR_CONFUSIONS = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5"}

_SETTLEMENT_TEMPLATES = [
    "NEFT CR-HDFC0000001-RAZORPAY SOFTWARE-{utr}-SETTLEMENT",
    "{utr} RAZORPAY SETTLEMENT",
    "NEFT/{utr}/RAZORPAYSOFTWAREPRIVATELIM",
    "IMPS/{utr}/RZPY",
    "NEFT CR: HDFC {utr} RAZORPAY SETTLEMENT",
]

# Deliberately carries NO UTR-shaped substring at all — used only for the
# force_unrecoverable case (seeded anomaly #12). A MANGLED UTR is not the
# same thing as a MISSING one: a mangled UTR is still, by construction,
# closer to its own true origin than to an unrelated settlement's UTR, so a
# fuzzy matcher relying on UTR closeness would still resolve it correctly —
# which would silently defeat the entire point of the ambiguous-duplicate
# anomaly. Only removing the UTR signal entirely forces genuine reliance on
# amount + date, which is where the real ambiguity lives.
_NO_UTR_TEMPLATES = [
    "NEFT CR: HDFC RAZORPAY SOFTWARE SETTLEMENT",
    "NEFT CR-HDFC0000001-RAZORPAY SETTLEMENT",
    "IMPS/RZPY/RAZORPAY SETTLEMENT CREDIT",
]

_NOISE_TEMPLATES = [
    ("UPI/DR/{ref}/SWIGGY/YESB/payment", "debit", (150_00, 1_200_00)),
    ("SALARY {month_label}", "debit", (35_000_00, 95_000_00)),
    ("NEFT DR-{vendor}-INVOICE {n}", "debit", (5_000_00, 80_000_00)),
    ("BANK CHARGES GST", "debit", (50_00, 500_00)),
    ("INT.CREDIT", "credit", (10_00, 300_00)),
]

_VENDORS = ["SHREEPACKAGING", "BLUEDARTEXP", "AWSINDIA", "GOOGLECLOUD", "OFFICESUPPLYCO"]

OPENING_BALANCE_PAISE = 20_00_000_00  # ₹20,00,000 — arbitrary but documented


def _mangle_utr(rng: random.Random, utr: str) -> str:
    """Simulate OCR/transcription-style corruption: 1-2 confusable-character
    swaps (0<->O, 1<->I, 5<->S) — the exact confusions Pass 4's fuzzy
    matcher is weighted to treat as cheap edits."""
    chars = list(utr)
    swappable = [i for i, c in enumerate(chars) if c in _UTR_CONFUSIONS]
    if not swappable:
        return utr
    n_swaps = min(len(swappable), rng.choice([1, 2]))
    for i in rng.sample(swappable, k=n_swaps):
        chars[i] = _UTR_CONFUSIONS[chars[i]]
    return "".join(chars)


def _settlement_narration(rng: random.Random, utr: str, mangled: bool,
                           force_unrecoverable: bool = False) -> tuple[str, str]:
    if force_unrecoverable:
        narration = rng.choice(_NO_UTR_TEMPLATES)[:50]
        return narration, ""
    template = rng.choice(_SETTLEMENT_TEMPLATES)
    display_utr = _mangle_utr(rng, utr) if mangled else utr
    narration = template.format(utr=display_utr)[:50]  # real narrations truncate

    # If the 50-char truncation actually cut into the UTR itself — some
    # template + UTR-style combinations run long enough to overflow before
    # the UTR is fully written out — ref_no MUST carry the full UTR as a
    # fallback. Without this, a genuinely unresolvable case can occur by
    # pure accident of template/UTR-length arithmetic rather than by
    # deliberate design: one such case, on a 101-line settlement batch,
    # dropped an otherwise-clean test-set match rate by ten points for a
    # reason that had nothing to do with any seeded anomaly. Real
    # information loss should only ever come from the anomalies actually
    # designed to cause it (#10, #12), not from incidental template-length
    # overflow. See FAILURE_LOG.md.
    if display_utr not in narration:
        ref_no = display_utr
    else:
        ref_no = display_utr if rng.random() < 0.7 else ""  # bank doesn't always populate ref_no
    return narration, ref_no


def _noise_row(rng: random.Random, d: date, idx: int) -> tuple[str, str, int]:
    template, direction, (lo, hi) = rng.choice(_NOISE_TEMPLATES)
    amount = rng.randint(lo, hi)
    narration = template.format(
        ref="".join(rng.choices(string.ascii_uppercase + string.digits, k=10)),
        month_label=d.strftime("%b %Y").upper(),
        vendor=rng.choice(_VENDORS),
        n=rng.randint(1000, 9999),
    )[:50]
    return narration, direction, amount


@dataclass
class BankGenResult:
    rows: list[BankRow]
    # settlement_id -> bank_txn_id(s) that carry its credit. Usually one;
    # two when seeded anomaly #13 (duplicate-then-reversed) hits that
    # settlement — the reversal row itself maps to no settlement at all.
    settlement_to_bank_txn: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def generate_bank_statement(
    rng: random.Random,
    summaries: list[SettlementSummary],
    rates: AnomalyRates,
    month: str,
    n_noise_rows: int = 25,
    force_unrecoverable_utr_ids: frozenset[str] = frozenset(),
) -> BankGenResult:
    """
    `force_unrecoverable_utr_ids`: settlement ids (both sides of a seeded
    anomaly #12 collision) that must have their UTR fully obscured in the
    bank narration — no exact match, no ref_no fallback. Without this, both
    twins would carry an intact, distinct UTR and Pass 1 would resolve them
    trivially, which would make the "ambiguous duplicate" case fake. This is
    what forces the matcher to fall back to amount+date and genuinely face
    two equally valid candidates.
    """
    year, mon = (int(x) for x in month.split("-"))

    # --- one credit row per settlement, with possible value-date skew and
    #     possible UTR mangling ---
    raw_rows: list[dict] = []
    for summary in summaries:
        # Everything is encoded at UTC noon at generation time (see
        # settlement.py's _to_unix) specifically so this conversion back to
        # a calendar date is unambiguous regardless of the machine's local
        # timezone running this code.
        settle_date = datetime.fromtimestamp(summary.created_at, tz=timezone.utc).date()
        skewed = rng.random() < rates.p_value_date_skew
        txn_date = next_business_day(settle_date) if skewed else settle_date

        force_unrecoverable = summary.id in force_unrecoverable_utr_ids
        # force_unrecoverable is now categorically different from ordinary
        # mangling (whole-UTR removal, not character-level corruption), so
        # it no longer implies the "mangled" tag/rate — the two are mutually
        # exclusive at the row level, not layered.
        mangled = (not force_unrecoverable) and rng.random() < rates.p_mangled_utr_in_narration
        narration, ref_no = _settlement_narration(rng, summary.utr, mangled, force_unrecoverable)
        if force_unrecoverable:
            ref_no = None  # no clean fallback field either

        anomaly_tags = []
        if skewed:
            anomaly_tags.append("seeded_11_value_date_skew")
        if mangled:
            anomaly_tags.append("seeded_10_mangled_utr")
        if force_unrecoverable:
            anomaly_tags.append("seeded_12_ambiguous_duplicate_amount")

        # A settlement batch CAN net negative — e.g. a quiet day where
        # refunds and fees outweigh new payments. When that happens Razorpay
        # debits the merchant's account to recover the shortfall rather than
        # crediting it. Silently clamping this to a zero-value row (an
        # earlier version of this function did exactly that) would make a
        # real net-debit day invisible in the bank statement — precisely
        # the kind of silent-wrong-answer bug this whole project exists to
        # catch. See FAILURE_LOG.md.
        if summary.amount >= 0:
            debit_paise, credit_paise = 0, summary.amount
        else:
            debit_paise, credit_paise = -summary.amount, 0

        raw_rows.append({
            "txn_date": txn_date,
            "value_date": settle_date,
            "narration": narration,
            "ref_no": ref_no or None,
            "debit_paise": debit_paise,
            "credit_paise": credit_paise,
            "anomaly_tags": anomaly_tags,
            "_settlement_id": summary.id,  # generation-time only, not a real bank field
        })

    # --- seeded anomaly #13: a duplicate credit row, later reversed ---
    n_dupes = min(rates.n_duplicate_bank_row_reversed_per_month, len(raw_rows))
    if n_dupes > 0:
        for original in rng.sample(raw_rows, k=n_dupes):
            # Only the DUPLICATE row is anomalous — the original credit is a
            # perfectly legitimate settlement and should remain a normal,
            # cleanly-matchable row. Tagging both would wrongly tell the
            # evaluator that a correct match on the original is somehow
            # still "expected to be an exception."
            dup = dict(original)
            dup["anomaly_tags"] = list(set(original["anomaly_tags"] + ["seeded_13_duplicate_reversed"]))
            raw_rows.append(dup)
            raw_rows.append({
                "txn_date": original["txn_date"] + timedelta(days=1),
                "value_date": original["txn_date"] + timedelta(days=1),
                "narration": "REVERSAL - DUPLICATE CREDIT",
                "ref_no": original["ref_no"],
                "debit_paise": original["credit_paise"],
                "credit_paise": 0,
                "anomaly_tags": ["seeded_13_duplicate_reversed"],
                "_settlement_id": None,
            })

    # --- noise rows, scattered across the month, unrelated to any order ---
    import calendar as _cal
    days_in_month = _cal.monthrange(year, mon)[1]
    for i in range(n_noise_rows):
        d = date(year, mon, rng.randint(1, days_in_month))
        narration, direction, amount = _noise_row(rng, d, i)
        raw_rows.append({
            "txn_date": d,
            "value_date": d,
            "narration": narration,
            "ref_no": None,
            "debit_paise": amount if direction == "debit" else 0,
            "credit_paise": amount if direction == "credit" else 0,
            "anomaly_tags": ["noise"],
            "_settlement_id": None,
        })

    # --- sort chronologically, assign ids, compute the running balance ---
    raw_rows.sort(key=lambda r: (r["txn_date"], r["value_date"]))
    balance = OPENING_BALANCE_PAISE
    bank_rows: list[BankRow] = []
    settlement_to_bank_txn: dict[str, list[str]] = defaultdict(list)

    for idx, r in enumerate(raw_rows, start=1):
        balance += r["credit_paise"] - r["debit_paise"]
        bank_txn_id = f"BNK-{idx:05d}"
        bank_rows.append(BankRow(
            bank_txn_id=bank_txn_id,
            txn_date=r["txn_date"],
            value_date=r["value_date"],
            narration=r["narration"],
            ref_no=r["ref_no"],
            debit_paise=r["debit_paise"],
            credit_paise=r["credit_paise"],
            balance_paise=balance,
            anomaly_tags=r["anomaly_tags"],
        ))
        if r["_settlement_id"] is not None:
            settlement_to_bank_txn[r["_settlement_id"]].append(bank_txn_id)

    return BankGenResult(rows=bank_rows, settlement_to_bank_txn=settlement_to_bank_txn)
