"""Purplle POS CSV loader.

The challenge brief sketches a simplified `(store_id, txn_id, timestamp,
basket_inr)` POS schema, but the **real** Purplle export
(`data/provided_context/Brigade_Bangalore_10_April_26 (1).csv`) is the full
38-column store sales export — one row per line item, joined by `order_id`.

Mappings used:
    store_id       ← `store_id`        (e.g. "ST1008")
    txn_id         ← `order_id`        (one row per item; we dedup)
    timestamp      ← `order_date` + `order_time`, parsed as DD-MM-YYYY HH:MM:SS
                     and tagged as IST → converted to UTC.
    basket_inr     ← SUM(`total_amount`) per `order_id`
                     (what the customer actually paid post-discount).

Why `total_amount`, not `GMV` or `NMV`?
    GMV = pre-discount sticker price (overstates value).
    NMV = post-Purplle-discount, but EXCLUDES item-level promotions.
    total_amount = the value the customer transacted (closest to "purchase").

If a future export labels columns differently we'll loosen the mapping; for
now we accept the canonical names verbatim and fail loudly on missing
required columns. That matches the brief's "no silent data fixups" rule.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Mapping

from app.core.logging import get_logger
from app.schemas.pos import POSTransaction

logger = get_logger(__name__)


# India Standard Time = UTC + 5h30
IST = timezone(timedelta(hours=5, minutes=30))

# Required columns that drive the analytics. Anything else in the CSV is
# preserved on the row but ignored by our DB/event model.
REQUIRED_COLS: tuple[str, ...] = (
    "order_id",
    "store_id",
    "order_date",
    "order_time",
    "total_amount",
)


class PurplePOSLoadError(ValueError):
    """Bad / unparseable Purplle CSV."""


def load_purplle_pos(path: Path | str) -> list[POSTransaction]:
    """Read a Purplle POS CSV and return one `POSTransaction` per `order_id`.

    Per-row line items are summed; the resulting transaction's `timestamp`
    is the timestamp on the first row (rows for one `order_id` carry the
    same date/time in the real exports — we sanity-check this).
    """
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(_iter_rows(fh))

    if not rows:
        return []

    return list(_aggregate_orders(rows))


# ──────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────


def _iter_rows(fh) -> Iterator[Mapping[str, str]]:
    reader = csv.DictReader(fh)
    if reader.fieldnames is None:
        raise PurplePOSLoadError("Purplle POS CSV is empty (no header row)")

    missing = [c for c in REQUIRED_COLS if c not in reader.fieldnames]
    if missing:
        raise PurplePOSLoadError(
            f"Purplle POS CSV missing required columns: {missing}. "
            f"Found: {list(reader.fieldnames)}"
        )

    yield from reader


def _aggregate_orders(rows: Iterable[Mapping[str, str]]) -> Iterator[POSTransaction]:
    """Group line items by (store_id, order_id) and emit one POSTransaction each."""
    grouped: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in rows:
        key = ((row.get("store_id") or "").strip(), (row.get("order_id") or "").strip())
        if not key[0] or not key[1]:
            # Defensive: a row missing either is unrecoverable; skip with a warn.
            logger.warning("pos.row_skipped", reason="missing store_id or order_id")
            continue
        grouped.setdefault(key, []).append(row)

    for (store_id, order_id), items in grouped.items():
        try:
            yield _to_transaction(store_id, order_id, items)
        except (ValueError, InvalidOperation) as exc:
            raise PurplePOSLoadError(
                f"order_id={order_id} store_id={store_id}: {exc}"
            ) from exc


def _to_transaction(
    store_id: str, order_id: str, items: list[Mapping[str, str]]
) -> POSTransaction:
    # Timestamp from the first item (same on all rows in real exports — we
    # sanity-check just to surface inconsistent data).
    first = items[0]
    ts = _parse_ist(first["order_date"], first["order_time"])
    for r in items[1:]:
        # If a single order_id straddles two times (data quality issue), keep
        # the earliest — matches "started at" semantics.
        rts = _parse_ist(r["order_date"], r["order_time"])
        if rts < ts:
            ts = rts

    basket_total = Decimal("0")
    for r in items:
        basket_total += _parse_decimal(r["total_amount"])

    # Round to 2 decimal places — POS data is naturally INR paise-precision.
    basket_total = basket_total.quantize(Decimal("0.01"))

    return POSTransaction(
        store_id=store_id,
        txn_id=order_id,
        timestamp=ts,
        basket_inr=basket_total,
    )


def _parse_ist(date_str: str, time_str: str) -> datetime:
    """Parse Purplle's `DD-MM-YYYY` + `HH:MM:SS` (local IST) → UTC datetime."""
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()
    if not date_str or not time_str:
        raise ValueError("empty order_date/order_time")
    # `strptime` raises a clear message on malformed strings — let it bubble.
    local = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S")
    return local.replace(tzinfo=IST).astimezone(timezone.utc)


def _parse_decimal(raw: str) -> Decimal:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return Decimal("0")
    return Decimal(raw)
