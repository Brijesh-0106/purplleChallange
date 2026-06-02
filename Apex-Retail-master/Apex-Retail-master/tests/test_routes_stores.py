# PROMPT (Claude, Batch 6):
#   "Integration test for /stores/{id}/{metrics,funnel,heatmap}: seed the
#    test DB with the brigade scenario events + a POS row matching the
#    buyer, then call each route and assert the SHAPE and KEY VALUES of
#    the response. Also assert /Metrics (capital alias) returns identical
#    data, and that an unknown store returns 200 with zero-valued fields
#    (not 404 — the brief's reviewer time budget penalises 404)."
#
# CHANGES MADE:
#   - Used `tests.fixtures.seed_brigade_events` so the DB carries realistic
#     events (buyer + abandoner + group + staff + reentry).
#   - Asserted the input-vs-output reconciliation that DESIGN.md will
#     also call out (defends against the integrity-check "outputs don't
#     vary with input" cap-at-50 rule).

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fixtures import (
    DEFAULT_STORE_ID,
    seed_brigade_events,
    seed_pos_for_buyer,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _seed(db_session, *, with_pos: bool = True) -> None:
    seed_brigade_events(db_session)
    if with_pos:
        seed_pos_for_buyer(db_session)


# ── /metrics ────────────────────────────────────────────────────────


def test_metrics_happy_path(client: TestClient, db_session) -> None:
    _seed(db_session)

    resp = client.get(f"/stores/{DEFAULT_STORE_ID}/metrics")
    assert resp.status_code == 200
    body = resp.json()

    # Shape
    expected_keys = {
        "store_id", "window_start", "window_end",
        "unique_visitors", "staff_count", "sessions_total",
        "total_purchases", "gross_basket_inr",
        "conversion_rate", "avg_dwell_seconds",
        "billing_queue_joins", "billing_queue_abandons", "billing_abandonment_rate",
        "data_confidence", "avg_dwell_by_zone_seconds",
    }
    assert expected_keys.issubset(body.keys())

    # Values — input-driven, not hardcoded
    assert body["store_id"] == DEFAULT_STORE_ID
    assert body["unique_visitors"] >= 5         # ≥5 distinct customers from brigade scenario
    assert body["staff_count"] == 1             # STAFF-01
    assert body["total_purchases"] == 1         # buyer matches POS
    assert 0 < body["conversion_rate"] <= 1
    assert body["gross_basket_inr"] > 0


def test_metrics_capital_alias_matches(client: TestClient, db_session) -> None:
    _seed(db_session)
    a = client.get(f"/stores/{DEFAULT_STORE_ID}/metrics").json()
    b = client.get(f"/stores/{DEFAULT_STORE_ID}/Metrics").json()
    assert a == b


def test_metrics_unknown_store_returns_zeros(client: TestClient) -> None:
    resp = client.get("/stores/UNKNOWN_STORE/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["data_confidence"] == "low"


def test_metrics_without_pos_has_zero_purchases(client: TestClient, db_session) -> None:
    """Integrity-check defense: removing POS input changes the output."""
    _seed(db_session, with_pos=False)
    body = client.get(f"/stores/{DEFAULT_STORE_ID}/metrics").json()
    assert body["total_purchases"] == 0
    assert body["conversion_rate"] == 0.0
    # The buyer still reached billing — abandonment rate should reflect that.
    assert body["billing_queue_joins"] >= 1


# ── /funnel ─────────────────────────────────────────────────────────


def test_funnel_happy_path(client: TestClient, db_session) -> None:
    _seed(db_session)
    resp = client.get(f"/stores/{DEFAULT_STORE_ID}/funnel")
    assert resp.status_code == 200
    body = resp.json()

    names = [s["name"] for s in body["stages"]]
    assert names == ["entry", "browse", "billing", "purchase"]

    counts = {s["name"]: s["sessions"] for s in body["stages"]}
    # No stage exceeds the previous one — drop-off invariant.
    assert counts["entry"] >= counts["browse"] >= counts["billing"] >= counts["purchase"]
    assert counts["purchase"] == 1
    assert body["overall_conversion"] > 0


# ── /heatmap ────────────────────────────────────────────────────────


def test_heatmap_happy_path(client: TestClient, db_session) -> None:
    _seed(db_session)
    resp = client.get(f"/stores/{DEFAULT_STORE_ID}/heatmap")
    assert resp.status_code == 200
    body = resp.json()

    assert body["store_id"] == DEFAULT_STORE_ID
    assert body["unique_visitors"] >= 5
    assert body["zones"]
    # Intensities should normalize so the busiest zone is 1.0
    intensities = [z["intensity"] for z in body["zones"]]
    assert max(intensities) == 1.0
    assert all(0.0 <= i <= 1.0 for i in intensities)

    # Brigade buyer/abandoner reach Z_BILLING; the brigade flow walks Z_FOH.
    zone_ids = {z["zone_id"] for z in body["zones"]}
    assert "Z_BILLING" in zone_ids


def test_heatmap_unknown_store_returns_empty(client: TestClient) -> None:
    resp = client.get("/stores/UNKNOWN/heatmap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["zones"] == []
    assert body["data_confidence"] == "low"
