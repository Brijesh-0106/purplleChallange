# PROMPT (Claude, Batch 6):
#   "Test compute_heatmap: (1) per-zone visits aggregated correctly,
#    (2) intensity normalised 0..1 with busiest=1.0, (3) staff excluded,
#    (4) data_confidence flips to low when sessions < threshold,
#    (5) zero sessions returns empty zones list."

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.heatmap import compute_heatmap
from app.services.sessions import Session

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _session(pid, *, is_staff=False, zones=None, dwells=None) -> Session:
    s = Session(
        store_id="ST1008",
        person_id=pid,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=60),
        is_staff=is_staff,
    )
    s.zones_visited.update(zones or [])
    s.dwell_seconds_by_zone.update(dwells or {})
    return s


def test_visits_and_intensity_normalised() -> None:
    sessions = [
        _session("P-1", zones={"Z_FOH"}, dwells={"Z_FOH": 10}),
        _session("P-2", zones={"Z_FOH", "Z_BILLING"}, dwells={"Z_FOH": 20, "Z_BILLING": 5}),
        _session("P-3", zones={"Z_FOH"}, dwells={"Z_FOH": 30}),
    ]
    h = compute_heatmap(sessions, store_id="ST1008")
    by_id = {z.zone_id: z for z in h.zones}
    assert by_id["Z_FOH"].visits == 3
    assert by_id["Z_BILLING"].visits == 1
    assert by_id["Z_FOH"].intensity == 1.0
    assert 0 < by_id["Z_BILLING"].intensity < 1


def test_zero_sessions_returns_empty_zones() -> None:
    h = compute_heatmap([], store_id="ST1008")
    assert h.zones == []
    assert h.unique_visitors == 0
    assert h.data_confidence == "low"


def test_staff_excluded() -> None:
    sessions = [
        _session("P-1", zones={"Z_FOH"}, dwells={"Z_FOH": 10}),
        _session("STAFF-01", is_staff=True, zones={"Z_FOH"}, dwells={"Z_FOH": 1000}),
    ]
    h = compute_heatmap(sessions, store_id="ST1008")
    [foh] = h.zones
    assert foh.visits == 1
    assert foh.avg_dwell_s == 10.0


def test_visit_share() -> None:
    sessions = [
        _session("P-1", zones={"Z_FOH"}),
        _session("P-2", zones={"Z_FOH", "Z_BILLING"}),
        _session("P-3", zones=set()),  # entered, didn't visit a zone
    ]
    h = compute_heatmap(sessions, store_id="ST1008")
    by_id = {z.zone_id: z for z in h.zones}
    assert by_id["Z_FOH"].visit_share == round(2 / 3, 4)
    assert by_id["Z_BILLING"].visit_share == round(1 / 3, 4)


def test_data_confidence_threshold() -> None:
    few = [_session(f"P-{i}", zones={"Z_FOH"}) for i in range(5)]
    many = [_session(f"P-{i}", zones={"Z_FOH"}) for i in range(25)]
    assert compute_heatmap(few, store_id="ST1008").data_confidence == "low"
    assert compute_heatmap(many, store_id="ST1008").data_confidence == "ok"
