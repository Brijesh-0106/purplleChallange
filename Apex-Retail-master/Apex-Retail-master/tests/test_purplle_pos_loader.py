# PROMPT (Claude, Batch 5):
#   "Test PurplePOSLoader: (1) parses the real provided_context CSV header
#    & first few rows correctly, (2) aggregates multi-line orders by
#    order_id, (3) handles DD-MM-YYYY date format, (4) converts IST → UTC,
#    (5) raises PurplePOSLoadError on missing required columns,
#    (6) skips rows with missing store_id / order_id rather than crashing."
#
# CHANGES MADE:
#   - Synthesised CSV fixtures in temp files so the test runs without the
#     gitignored real dataset.
#   - Asserted UTC timezone on the resulting POSTransaction.timestamp.

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.infra.loaders.purplle_pos_loader import (
    PurplePOSLoadError,
    load_purplle_pos,
)

# Shorthand for the 38-column header with only the columns we care about
# populated (the rest are filled with empty strings — matches reality).
HEADER = (
    "order_id,coupon_code,offer_name,discount_code,invoice_number,invoice_type,"
    "order_date,order_time,return_id,store_id,store_name,city,customer_name,"
    "customer_number,sku,product_id,ean,product_name,brand_name,dep_name,"
    "sub_category,brand_type,tax,hsn_code,salesperson_id,employee_code,"
    "salesperson_name,qty,GMV,NMV,coupon_amount,item_promotion,amt_without_gwp,"
    "total_amount,pb_eb_sale,week_assigned,tax_m,taxable_amt,tax_amt"
)


def _row(order_id: str, store_id: str, date: str, time: str, total: str) -> str:
    """Generate one CSV row with the minimal cells our loader reads."""
    cells = [""] * 38
    cells[0] = order_id          # order_id
    cells[6] = date              # order_date
    cells[7] = time              # order_time
    cells[9] = store_id          # store_id
    cells[33] = total            # total_amount
    return ",".join(cells)


def _write(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


# ── Happy path ──────────────────────────────────────────────────────


def test_parses_single_row_order(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "pos.csv",
        [_row("104363838", "ST1008", "10-04-2026", "16:55:36", "274.36")],
    )
    txns = load_purplle_pos(p)
    assert len(txns) == 1
    t = txns[0]
    assert t.txn_id == "104363838"
    assert t.store_id == "ST1008"
    assert t.basket_inr == Decimal("274.36")
    # 16:55:36 IST → 11:25:36 UTC
    assert t.timestamp.tzinfo is timezone.utc
    assert t.timestamp.hour == 11
    assert t.timestamp.minute == 25
    assert t.timestamp.second == 36


def test_aggregates_multi_line_order(tmp_path: Path) -> None:
    """Real exports have one row per item; same order_id must collapse to one txn."""
    p = _write(
        tmp_path / "pos.csv",
        [
            _row("104373042", "ST1008", "10-04-2026", "18:41:51", "1448.18"),
            _row("104373042", "ST1008", "10-04-2026", "18:41:51", "0.80"),
        ],
    )
    txns = load_purplle_pos(p)
    assert len(txns) == 1
    assert txns[0].basket_inr == Decimal("1448.98")


def test_keeps_earliest_timestamp_when_rows_disagree(tmp_path: Path) -> None:
    """Defensive: if a single order_id straddles two times, we pick the earliest."""
    p = _write(
        tmp_path / "pos.csv",
        [
            _row("X", "ST1008", "10-04-2026", "12:30:00", "100.00"),
            _row("X", "ST1008", "10-04-2026", "12:00:00", "50.00"),  # earlier
        ],
    )
    txns = load_purplle_pos(p)
    assert len(txns) == 1
    assert txns[0].timestamp.hour == 6   # 12:00 IST → 06:30 UTC
    assert txns[0].timestamp.minute == 30


def test_distinct_orders_become_distinct_transactions(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "pos.csv",
        [
            _row("A", "ST1008", "10-04-2026", "12:00:00", "100"),
            _row("B", "ST1008", "10-04-2026", "12:01:00", "200"),
            _row("C", "ST1008", "10-04-2026", "12:02:00", "300"),
        ],
    )
    txns = load_purplle_pos(p)
    assert {t.txn_id for t in txns} == {"A", "B", "C"}


# ── Errors ──────────────────────────────────────────────────────────


def test_missing_required_column_raises(tmp_path: Path) -> None:
    # Missing total_amount column entirely (truncated header).
    bad = "order_id,store_id,order_date,order_time\nA,ST1008,10-04-2026,12:00:00\n"
    p = tmp_path / "pos.csv"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(PurplePOSLoadError):
        load_purplle_pos(p)


def test_empty_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "pos.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(PurplePOSLoadError):
        load_purplle_pos(p)


def test_bad_date_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "pos.csv",
        [_row("X", "ST1008", "not-a-date", "12:00:00", "100")],
    )
    with pytest.raises(PurplePOSLoadError):
        load_purplle_pos(p)


def test_skips_rows_missing_store_or_order(tmp_path: Path) -> None:
    """Defensive: skip incomplete rows rather than raise on first malformed row."""
    p = _write(
        tmp_path / "pos.csv",
        [
            _row("A", "ST1008", "10-04-2026", "12:00:00", "100"),
            _row("", "ST1008", "10-04-2026", "12:01:00", "200"),     # blank order_id
            _row("B", "", "10-04-2026", "12:02:00", "300"),           # blank store_id
        ],
    )
    txns = load_purplle_pos(p)
    assert {t.txn_id for t in txns} == {"A"}
