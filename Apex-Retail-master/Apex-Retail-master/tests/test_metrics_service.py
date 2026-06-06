# PROMPT (Claude, Batch 6):
#   "Test compute_store_metrics: (1) zero sessions → zero unique visitors,
#    conversion 0 (not NaN), data_confidence='low', (2) staff filtered out,
#    (3) conversion = purchasers / visitors, (4) avg dwell only across
#    customers with dwell > 0, (5) per-zone dwell means."
#
# CHANGES MADE:
#   - Tested zero-session bounds and conversion rates calculation.
#   - Excluded staff from metrics and validated average dwell times and per-zone averages.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.metrics import compute_store_metrics
from app.services.sessions import Session


T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _session(person_id, *, is_staff=False, billed=False, purchase=False, dwells=None):
    s = Session(
        store_id="ST1008",
        person_id=person_id,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=60),
        is_staff=is_staff,
    )
    if billed:
        s.billing_join_count = 1
    if purchase:
        s.purchase = True
        s.purchase_basket_inr = 999.0
    s.dwell_seconds_by_zone.update(dwells or {})
    return s


def test_zero_sessions_zero_metrics() -> None:
    m = compute_store_metrics([], store_id="ST1008")
    assert m.unique_visitors == 0
    assert m.conversion_rate == 0.0
    assert m.data_confidence == "low"


def test_conversion_rate_uses_unique_visitors() -> None:
    sessions = [
        _session("P-1", billed=True, purchase=True, dwells={"Z_FOH": 10}),
        _session("P-2", billed=True, purchase=False, dwells={"Z_FOH": 30}),
        _session("P-3", billed=False, dwells={"Z_FOH": 20}),
    ]
    m = compute_store_metrics(sessions, store_id="ST1008")
    assert m.unique_visitors == 3
    assert m.total_purchases == 1
    assert m.conversion_rate == round(1 / 3, 4)
    assert m.gross_basket_inr == 999.0


def test_staff_excluded_from_visitor_count() -> None:
    sessions = [
        _session("P-1", dwells={"Z_FOH": 10}),
        _session("STAFF-01", is_staff=True, dwells={"Z_FOH": 1000}),  # staff dwell ignored
    ]
    m = compute_store_metrics(sessions, store_id="ST1008")
    assert m.unique_visitors == 1
    assert m.staff_count == 1
    # avg dwell over CUSTOMERS only.
    assert m.avg_dwell_seconds == 10.0


def test_billing_abandonment_rate() -> None:
    sessions = [
        _session("P-1", billed=True, purchase=True),
        _session("P-2", billed=True, purchase=False),
    ]
    sessions[1].billing_abandon_count = 1
    m = compute_store_metrics(sessions, store_id="ST1008")
    # 2 joins, 1 unmatched abandon.
    assert m.billing_queue_joins == 2
    assert m.billing_queue_abandons == 1
    assert m.billing_abandonment_rate == 0.5


def test_data_confidence_threshold() -> None:
    """Default 20-session threshold."""
    few = [_session(f"P-{i}", dwells={"Z_FOH": 10}) for i in range(5)]
    many = [_session(f"P-{i}", dwells={"Z_FOH": 10}) for i in range(25)]
    assert compute_store_metrics(few, store_id="ST1008").data_confidence == "low"
    assert compute_store_metrics(many, store_id="ST1008").data_confidence == "ok"


def test_per_zone_dwell_means() -> None:
    sessions = [
        _session("P-1", dwells={"Z_FOH": 10, "Z_MAKEUP": 30}),
        _session("P-2", dwells={"Z_FOH": 30}),
    ]
    m = compute_store_metrics(sessions, store_id="ST1008")
    assert m.avg_dwell_by_zone_seconds["Z_FOH"] == 20.0
    assert m.avg_dwell_by_zone_seconds["Z_MAKEUP"] == 30.0
