"""POS CSV loader.

Tolerates a couple of common header variants (`amount`/`total` for `basket_inr`,
`time`/`datetime` for `timestamp`). Anything else is rejected — silently
mapping unknown columns is exactly the kind of "data fixup" the brief
penalises.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO

from app.core.logging import get_logger
from app.schemas.pos import POSTransaction

logger = get_logger(__name__)

# Canonical → accepted alternates. First match wins.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "store_id": ("store_id", "store"),
    "txn_id": ("txn_id", "transaction_id", "txn"),
    "timestamp": ("timestamp", "time", "datetime", "ts"),
    "basket_inr": ("basket_inr", "amount", "total", "basket", "value"),
}


class POSLoadError(ValueError):
    """Raised when the file shape is unrecoverably wrong (missing columns, etc.)."""


def load_pos_csv(path: Path) -> list[POSTransaction]:
    """Read a POS CSV file. Returns validated transactions; raises on bad rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(_iter_pos_csv(fh))


def _iter_pos_csv(fh: IO[str]) -> Iterator[POSTransaction]:
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        raise POSLoadError("POS CSV is empty (no header row)")

    header_map = _resolve_headers(reader.fieldnames)

    for row_num, raw in enumerate(reader, start=2):  # +1 for header, +1 for 1-indexing
        try:
            yield POSTransaction(
                store_id=raw[header_map["store_id"]],
                txn_id=raw[header_map["txn_id"]],
                timestamp=_parse_ts(raw[header_map["timestamp"]]),
                basket_inr=_parse_decimal(raw[header_map["basket_inr"]]),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise POSLoadError(f"row {row_num}: {exc}") from exc


def _resolve_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map canonical names to whatever the file actually used.

    Case-insensitive on header names; preserves the original spelling for indexing.
    """
    lower_to_actual = {h.strip().lower(): h for h in fieldnames}
    resolved: dict[str, str] = {}
    missing: list[str] = []

    for canonical, candidates in _HEADER_ALIASES.items():
        match = next((lower_to_actual[c] for c in candidates if c in lower_to_actual), None)
        if match is None:
            missing.append(canonical)
        else:
            resolved[canonical] = match

    if missing:
        raise POSLoadError(
            f"POS CSV missing required columns: {missing}. "
            f"Found: {list(fieldnames)}"
        )
    return resolved


def _parse_ts(raw: str) -> datetime:
    """Accept ISO-8601, with or without timezone."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    # Python 3.11+ accepts trailing 'Z' in fromisoformat; older versions don't.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def _parse_decimal(raw: str) -> Decimal:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        raise ValueError("empty amount")
    return Decimal(raw)
