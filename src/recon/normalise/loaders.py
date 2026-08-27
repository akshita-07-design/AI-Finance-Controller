"""
Loaders for the three on-disk sources (plus ground truth, for the evaluator
only — never imported here by anything the matcher itself uses).

This module is the boundary: past this point, the matching engine only ever
sees what a real system would see on disk. No generation-time metadata
(order_id_map, payment_entity_map, etc.) survives past this file — if the
matcher needs something, it has to derive it from the same fields a real
Razorpay merchant would have.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from recon.models import BankRow, GroundTruth, LedgerRecord, SettlementLine, SettlementSummary


def _sanitize_record(record: dict) -> dict:
    """Replace pandas' float NaN with a real None in every field of a row
    dict. `df.where(pd.notna(df), None)` looks like it should do this at the
    DataFrame level but does NOT reliably survive `to_dict()` for object/
    string-dtype columns — the NaN comes back as a float `nan`, which fails
    Pydantic's Optional[...] validation with a confusing enum error rather
    than an obvious "missing value" one. Sanitizing at the dict level, after
    conversion, is the version that actually works."""
    return {
        k: (None if isinstance(v, float) and math.isnan(v) else v)
        for k, v in record.items()
    }


def load_ledger(path: Path) -> list[LedgerRecord]:
    df = pd.read_csv(path)
    return [LedgerRecord(**_sanitize_record(row)) for row in df.to_dict(orient="records")]


def load_settlement_lines(path: Path) -> list[SettlementLine]:
    with open(path) as f:
        payload = json.load(f)
    return [SettlementLine(**item) for item in payload["items"]]


def load_settlement_summaries(path: Path) -> list[SettlementSummary]:
    with open(path) as f:
        payload = json.load(f)
    return [SettlementSummary(**item) for item in payload]


def load_bank_statement(path: Path) -> list[BankRow]:
    df = pd.read_csv(path)
    rows = []
    for raw_row in df.to_dict(orient="records"):
        row = _sanitize_record(raw_row)
        # pandas reads the anomaly_tags list column back as a Python-repr
        # string ("['tag1', 'tag2']") rather than a real list — cheap to fix
        # here rather than pushing string-parsing into the model itself.
        tags = row.get("anomaly_tags")
        if isinstance(tags, str):
            row["anomaly_tags"] = json.loads(tags.replace("'", '"')) if tags != "[]" else []
        rows.append(BankRow(**row))
    return rows


def load_ground_truth(path: Path) -> GroundTruth:
    """Evaluator-only. The matching engine must never import this."""
    with open(path) as f:
        payload = json.load(f)
    return GroundTruth(**payload)


class DataSources:
    """Convenience bundle — one call to load everything the matcher needs
    from a `data/dev` or `data/test` directory."""

    def __init__(self, data_dir: Path):
        self.ledger: list[LedgerRecord] = load_ledger(data_dir / "internal_ledger.csv")
        self.settlement_lines: list[SettlementLine] = load_settlement_lines(
            data_dir / "settlement_recon.json"
        )
        self.settlement_summaries: list[SettlementSummary] = load_settlement_summaries(
            data_dir / "settlement_summaries.json"
        )
        self.bank_rows: list[BankRow] = load_bank_statement(data_dir / "bank_statement.csv")
