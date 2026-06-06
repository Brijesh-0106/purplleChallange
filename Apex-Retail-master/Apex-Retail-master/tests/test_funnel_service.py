# PROMPT (Claude, Batch 6):
#   "Test compute_funnel: (1) all 4 stages present in order, (2) drop-off
#    is non-negative, (3) staff sessions excluded, (4) overall_conversion
#    = purchase / entry, (5) zero sessions returns four zeros."
#
# CHANGES MADE:
#   - Tested 4-stage funnel computation order, counts, and drop-off bounds.
#   - Verified exclusion of staff and correct handling of zero-session edge cases.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.funnel import compute_funnel
from app.services.sessions import Session

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _session(pid, *, is_staff=False, browsed=False, billed=False, purchase=False) -> Session:
    s = Session(
        store_id="ST1008",
        person_id=pid,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=60),
        is_staff=is_staff,
    )
    if browsed:
        s.zones_visited.add("Z_FOH")
    if billed:
        s.billing_join_count = 1
        s.zones_visited.add("Z_BILLING")
    if purchase:
        s.purchase = True
    return s


def test_funnel_has_four_stages_in_order() -> None:
    f = compute_funnel([_session("P-1", browsed=True, billed=True, purchase=True)], store_id="ST1008")
    assert [s.name for s in f.stages] == ["entry", "browse", "billing", "purchase"]


def test_funnel_counts_descend_correctly() -> None:
    sessions = [
        _session("P-1", browsed=True, billed=True, purchase=True),
        _session("P-2", browsed=True, billed=True, purchase=False),
        _session("P-3", browsed=True, billed=False),
        _session("P-4"),  # entered but didn't browse (perimeter only)
    ]
    f = compute_funnel(sessions, store_id="ST1008")
    by_name = {s.name: s.sessions for s in f.stages}
    assert by_name == {"entry": 4, "browse": 3, "billing": 2, "purchase": 1}
    assert f.overall_conversion == 0.25


def test_drop_off_is_non_negative() -> None:
    """No stage should ever count higher than the previous one."""
    sessions = [
        _session(f"P-{i}", browsed=True, billed=(i < 3), purchase=(i < 1))
        for i in range(5)
    ]
    f = compute_funnel(sessions, store_id="ST1008")
    for s in f.stages:
        assert 0.0 <= s.drop_off_from_previous <= 1.0


def test_staff_excluded_from_funnel() -> None:
    sessions = [
        _session("P-1", browsed=True),
        _session("STAFF-01", is_staff=True, browsed=True, billed=True, purchase=True),
    ]
    f = compute_funnel(sessions, store_id="ST1008")
    by_name = {s.name: s.sessions for s in f.stages}
    assert by_name["entry"] == 1
    assert by_name["purchase"] == 0


def test_zero_sessions_zero_funnel() -> None:
    f = compute_funnel([], store_id="ST1008")
    by_name = {s.name: s.sessions for s in f.stages}
    assert by_name == {"entry": 0, "browse": 0, "billing": 0, "purchase": 0}
    assert f.overall_conversion == 0.0
