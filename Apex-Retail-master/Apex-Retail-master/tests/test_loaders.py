# PROMPT (Claude, Batch 2):
#   "Write loader tests using temp files: (1) POS happy path with canonical
#    headers, (2) POS with alias headers (`amount` instead of `basket_inr`),
#    (3) POS missing required column → POSLoadError, (4) layout happy path
#    with two stores and a non-trivial polygon, (5) layout malformed → error."
#
# CHANGES MADE:
#   - Used pytest's `tmp_path` to keep tests hermetic.
#   - Added a Z-suffix timestamp case to confirm parsing.

from __future__ import annotations

import json
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.infra.loaders.layout_loader import LayoutLoadError, load_store_layouts
from app.infra.loaders.pos_loader import POSLoadError, load_pos_csv


# ── POS loader ───────────────────────────────────────────────────────────


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


def test_pos_canonical_headers(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "pos.csv",
        "store_id,txn_id,timestamp,basket_inr\n"
        "STORE_BLR_002,T1,2026-05-31T12:00:00Z,499.50\n"
        "STORE_BLR_002,T2,2026-05-31T12:01:00+00:00,1200.00\n",
    )
    txns = load_pos_csv(csv_path)
    assert len(txns) == 2
    assert txns[0].txn_id == "T1"
    assert txns[0].basket_inr == Decimal("499.50")
    assert txns[0].timestamp.tzinfo is timezone.utc


def test_pos_alias_headers_accepted(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "pos.csv",
        "store,transaction_id,time,amount\n"
        "STORE_BLR_002,T9,2026-05-31T12:00:00Z,250.00\n",
    )
    txns = load_pos_csv(csv_path)
    assert txns[0].store_id == "STORE_BLR_002"
    assert txns[0].txn_id == "T9"
    assert txns[0].basket_inr == Decimal("250.00")


def test_pos_missing_column_raises(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "pos.csv",
        "store_id,txn_id,timestamp\nSTORE_BLR_002,T1,2026-05-31T12:00:00Z\n",
    )
    with pytest.raises(POSLoadError):
        load_pos_csv(csv_path)


def test_pos_bad_row_raises(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "pos.csv",
        "store_id,txn_id,timestamp,basket_inr\n"
        "STORE_BLR_002,T1,not-a-date,499.50\n",
    )
    with pytest.raises(POSLoadError):
        load_pos_csv(csv_path)


# ── Layout loader ───────────────────────────────────────────────────────


def test_layout_happy_path(tmp_path: Path) -> None:
    payload = {
        "STORE_BLR_002": {
            "zones": [
                {
                    "zone_id": "Z_ENTRY",
                    "name": "Entry",
                    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                    "is_billing": False,
                },
                {
                    "zone_id": "Z_BILLING",
                    "name": "Billing",
                    "polygon": [[20, 0], [40, 0], [40, 30], [20, 30]],
                    "is_billing": True,
                },
            ]
        }
    }
    p = tmp_path / "layout.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    layouts = load_store_layouts(p)
    layout = layouts["STORE_BLR_002"]
    assert len(layout.zones) == 2
    # Containment check (centroid of billing rectangle).
    billing = layout.zone_at(30, 15)
    assert billing is not None
    assert billing.zone_id == "Z_BILLING"
    assert billing.is_billing is True


def test_layout_polygon_too_small(tmp_path: Path) -> None:
    payload = {
        "STORE_BLR_002": {
            "zones": [{"zone_id": "Z_BAD", "polygon": [[0, 0], [1, 1]]}]
        }
    }
    p = tmp_path / "layout.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LayoutLoadError):
        load_store_layouts(p)


def test_layout_root_must_be_object(tmp_path: Path) -> None:
    p = tmp_path / "layout.json"
    p.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(LayoutLoadError):
        load_store_layouts(p)
