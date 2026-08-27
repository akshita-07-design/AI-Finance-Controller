"""
Data models for the three sources plus ground truth.

Kept in one file deliberately at this project size — splitting into
ledger_models.py / settlement_models.py / bank_models.py would be premature
structure for ~150 lines of schema. Revisit if this file crosses ~400 lines.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class PaymentMethod(str, Enum):
    UPI = "upi"
    NETBANKING = "netbanking"
    CARD_DEBIT = "card_debit"
    CARD_CREDIT = "card_credit"
    CARD_AMEX = "card_amex"
    WALLET = "wallet"


class OrderStatus(str, Enum):
    PAID = "paid"
    ABANDONED = "abandoned"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    ON_HOLD = "on_hold"


class Channel(str, Enum):
    WEB = "web"
    APP = "app"
    PAYMENT_LINK = "payment_link"


class SettlementEntityType(str, Enum):
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"


# ---------------------------------------------------------------------------
# Source A — internal order ledger
# ---------------------------------------------------------------------------

class LedgerRecord(BaseModel):
    internal_order_id: str          # e.g. "ORD-2026-04-00123" — merchant's own scheme
    order_receipt: str              # what gets passed to Razorpay as `receipt` — the bridge
    customer_id: str
    order_date: date
    gross_amount_paise: int         # what the customer was charged
    goods_gst_paise: int            # GST on the GOODS — distinct from GST on MDR, never touches recon
    status: OrderStatus
    channel: Channel
    sku_count: int
    payment_method: Optional[PaymentMethod] = None   # None if abandoned — never paid

    @field_validator("gross_amount_paise", "goods_gst_paise")
    @classmethod
    def _no_floats(cls, v):
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"money fields must be int paise, got {type(v).__name__}")
        return v


# ---------------------------------------------------------------------------
# Source B — settlement recon report (mirrors the real Razorpay schema)
# ---------------------------------------------------------------------------

class SettlementLine(BaseModel):
    """One line of `api->settlement->reports()` / `settlementRecon()`.

    Field names deliberately match Razorpay's actual API response so the
    synthetic data is structurally identical to the real thing.
    """
    entity_id: str                  # pay_... / rfnd_... / trf_... / adj_...
    type: SettlementEntityType
    debit: int
    credit: int
    amount: int
    currency: str = "INR"
    fee: int = 0
    tax: int = 0
    on_hold: bool = False
    settled: bool = True
    created_at: int                 # unix timestamp
    settled_at: Optional[int] = None
    settlement_id: Optional[str] = None
    posted_at: Optional[int] = None
    credit_type: Optional[str] = "default"
    description: Optional[str] = None
    notes: str = "{}"
    payment_id: Optional[str] = None       # populated on refund rows -> original payment
    settlement_utr: Optional[str] = None   # can be null even with a settlement_id (adjustments)
    order_id: Optional[str] = None
    order_receipt: Optional[str] = None    # the bridge back to the internal ledger
    method: Optional[PaymentMethod] = None
    card_network: Optional[str] = None
    card_issuer: Optional[str] = None
    card_type: Optional[str] = None
    dispute_id: Optional[str] = None

    # generation-time metadata — NOT part of the real Razorpay schema, used
    # only to build ground truth. Strip before treating this as "real" data
    # if you ever export it for someone unfamiliar with the project.
    anomaly_tags: list[str] = Field(default_factory=list)


class SettlementSummary(BaseModel):
    """One settlement batch — the entity behind `api->settlement->fetch()`."""
    id: str                         # setl_...
    entity: str = "settlement"
    amount: int                     # NET payout — must equal the bank credit
    status: str = "processed"
    fees: int
    tax: int
    utr: str
    created_at: int


# ---------------------------------------------------------------------------
# Source C — bank statement
# ---------------------------------------------------------------------------

class BankRow(BaseModel):
    bank_txn_id: str                # e.g. "BNK-0007" — our own row id, not a real bank field
    txn_date: date                  # posting date
    value_date: date                # can differ from posting date (settlement skew)
    narration: str                  # free text, UTR embedded and sometimes mangled
    ref_no: Optional[str] = None    # sometimes the UTR, sometimes blank, sometimes unrelated
    debit_paise: int = 0
    credit_paise: int = 0
    balance_paise: int              # running balance — self-consistency check for the generator

    anomaly_tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Ground truth — never read by the matching engine, only by the evaluator
# ---------------------------------------------------------------------------

class MatchType(str, Enum):
    THREE_WAY_EXACT = "three_way_exact"          # order <-> settlement <-> bank, all clean
    SETTLEMENT_BANK_ONLY = "settlement_bank_only" # e.g. a transfer with no order
    BATCH_LEVEL = "batch_level"                   # settlement summary <-> bank row


class GroundTruthMatch(BaseModel):
    match_id: str
    internal_order_id: Optional[str] = None
    settlement_entity_id: Optional[str] = None
    settlement_id: Optional[str] = None
    bank_txn_id: Optional[str] = None
    match_type: MatchType
    anomaly_tags: list[str] = Field(default_factory=list)


class ExpectedException(BaseModel):
    record_id: str
    reason_code: str
    anomaly_tags: list[str] = Field(default_factory=list)
    note: str = ""


class GeneratorConfig(BaseModel):
    seed: int
    month: str                      # "2026-04"
    n_orders: int
    anomaly_rates: dict[str, float]


class GroundTruth(BaseModel):
    generator_config: GeneratorConfig
    matches: list[GroundTruthMatch]
    expected_exceptions: list[ExpectedException]
