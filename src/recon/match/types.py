"""Shared result types for the matching engine, used across all passes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

class SettlementBankMatchMethod(str, Enum):
    EXACT_UTR = "exact_utr"
    FUZZY_UTR = "fuzzy_utr"


class VarianceClass(str, Enum):
    OK = "ok"
    ROUNDING_DRIFT = "rounding_drift"       # < ₹1 (100 paise)
    UNEXPLAINED = "unexplained"


@dataclass
class AmbiguousGroup:
    """One or more settlements/bank rows that couldn't be told apart.

    Nearly always exactly 2 settlement ids vs 2 bank txn ids in this
    project's data (seeded anomaly #12), but kept as lists rather than a
    fixed pair so a genuinely 3-way collision wouldn't silently corrupt the
    shape of this record.
    """
    settlement_ids: list[str]
    bank_txn_ids: list[str]
    reason: str


@dataclass
class P1Result:
    # settlement_id -> bank_txn_id, for pairs Pass 1 is CONFIDENT about
    matched: dict[str, str] = field(default_factory=dict)
    unresolved_settlement_ids: list[str] = field(default_factory=list)
    unresolved_bank_txn_ids: list[str] = field(default_factory=list)
    ambiguous: list[AmbiguousGroup] = field(default_factory=list)
    # settlement_id -> bank_txn_id(s) that carry the SAME UTR and SAME sign
    # as the resolved match, but are NOT the one picked — e.g. seeded
    # anomaly #13 (a duplicated credit, later reversed). This is a
    # DIFFERENT failure mode from `ambiguous`: here we know exactly what
    # happened (a good match exists, plus a redundant copy), whereas
    # `ambiguous` means we genuinely cannot tell which of several DIFFERENT
    # things a record corresponds to. Conflating the two would flag a
    # perfectly resolvable settlement as "unresolved" just because its bank
    # row happens to have an erroneous duplicate sitting next to it.
    duplicate_same_sign: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class P4Result:
    matched: dict[str, str] = field(default_factory=dict)          # settlement_id -> bank_txn_id
    match_method: dict[str, SettlementBankMatchMethod] = field(default_factory=dict)
    still_unresolved_settlement_ids: list[str] = field(default_factory=list)
    still_unresolved_bank_txn_ids: list[str] = field(default_factory=list)
    ambiguous: list[AmbiguousGroup] = field(default_factory=list)


class OrderLinkMethod(str, Enum):
    DIRECT_RECEIPT = "direct_receipt"           # payment line, joined on its own order_receipt
    PAYMENT_ID_CHAIN = "payment_id_chain"       # refund/adjustment, followed payment_id -> original
    NONE_EXPECTED = "none_expected"              # transfer — no order behind it, by design
    UNRESOLVED = "unresolved"                    # should have a link but none was found — a real problem


@dataclass
class P2Result:
    # entity_id -> internal_order_id (None if NONE_EXPECTED or UNRESOLVED)
    order_link: dict[str, str | None] = field(default_factory=dict)
    link_method: dict[str, OrderLinkMethod] = field(default_factory=dict)


@dataclass
class BatchVariance:
    settlement_id: str
    internal_net: int          # sum(credit) - sum(debit) over the batch's own lines
    summary_net: int           # what the settlement summary itself claims
    bank_net: int | None       # what the matched bank row shows, if matched at all
    variance_class: VarianceClass
    delta_paise: int           # bank_net - summary_net (0 if bank_net is None)


@dataclass
class P3Result:
    batch_variances: dict[str, BatchVariance] = field(default_factory=dict)


class AdjudicationDecision(str, Enum):
    MATCH = "match"
    NO_MATCH = "no_match"
    ESCALATE = "escalate"


@dataclass
class AdjudicationRecord:
    """One LLM call's full outcome — the audit trail entry. Kept even when
    the proposal is rejected, since a rejected proposal (and WHY it was
    rejected) is exactly the kind of evidence a reviewer wants to see."""
    bank_txn_id: str
    candidate_settlement_ids: list[str]
    raw_decision: AdjudicationDecision | None    # None if the response didn't even parse
    raw_candidate_id: str | None
    raw_confidence: float | None
    reasoning: str
    evidence: list[str]
    accepted_match: tuple[str, str] | None       # (settlement_id, bank_txn_id) if accepted
    rejection_reason: str | None                 # None if accepted
    prompt_hash: str
    latency_ms: float
    from_cache: bool
    input_tokens: int | None = None    # None for cache hits — no real call was made
    output_tokens: int | None = None


@dataclass
class P5Result:
    matched: dict[str, str] = field(default_factory=dict)   # settlement_id -> bank_txn_id
    records: list[AdjudicationRecord] = field(default_factory=list)
