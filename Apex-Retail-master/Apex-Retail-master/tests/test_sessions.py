# PROMPT (Claude, Batch 6):
#   "Test build_sessions: (1) ENTRY+EXIT becomes one session, (2) REENTRY
#    inside session does NOT open a new session, (3) gap > session_gap_minutes
#    splits into two sessions, (4) staff is_staff sticks, (5) DWELL events
#    accumulate into dwell_seconds_by_zone, (6) BILLING_QUEUE_JOIN sets
#    reached_billing=True, (7) malformed rows (no person_id, no track_id)
#    are skipped."
#
# CHANGES MADE:
#   - Authored events as plain dicts (matches what `fetch_for_analytics`
#     returns) so the test exercises the actual contract.
#   - Used Brigade zone IDs (Z_FOH, Z_BILLING) for realism.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.sessions import build_sessions

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ev(t_offset_s: float, et: str, *, person="P-1", store="ST1008", **extra):
    return {
        "store_id": store,
        "person_id": person,
        "track_id": person,
        "event_type": et,
        "timestamp": T0 + timedelta(seconds=t_offset_s),
        "zone_id": extra.get("zone_id"),
        "duration_s": extra.get("duration_s"),
        "is_staff": extra.get("is_staff", False),
    }


# ── Single-session basics ────────────────────────────────────────────


def test_entry_exit_makes_one_session() -> None:
    events = [_ev(0, "ENTRY"), _ev(60, "EXIT")]
    sessions = build_sessions(events)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.entry_count == 1
    assert s.is_open is False
    assert s.duration_s == 60.0


def test_reentry_within_session_does_not_split() -> None:
    """V-002 leaves and comes back within 10 min → still ONE session."""
    events = [
        _ev(0, "ENTRY"),
        _ev(30, "EXIT"),
        _ev(120, "REENTRY"),     # 90 s after exit, < 10 min gap
        _ev(180, "EXIT"),
    ]
    sessions = build_sessions(events)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.entry_count == 1
    assert s.reentry_count == 1
    assert s.duration_s == 180.0


def test_gap_over_threshold_splits_into_two_sessions() -> None:
    events = [
        _ev(0, "ENTRY"),
        _ev(60, "EXIT"),
        _ev(60 + 11 * 60, "ENTRY"),  # 11 min later → new session
        _ev(60 + 12 * 60, "EXIT"),
    ]
    sessions = build_sessions(events, session_gap_minutes=10)
    assert len(sessions) == 2


# ── Field accumulation ──────────────────────────────────────────────


def test_dwell_accumulates_per_zone() -> None:
    events = [
        _ev(0, "ENTRY"),
        _ev(5, "ZONE_ENTER", zone_id="Z_FOH"),
        _ev(20, "DWELL", zone_id="Z_FOH", duration_s=15.0),
        _ev(25, "ZONE_ENTER", zone_id="Z_MAKEUP"),
        _ev(45, "DWELL", zone_id="Z_MAKEUP", duration_s=20.0),
        _ev(50, "EXIT"),
    ]
    [s] = build_sessions(events)
    assert s.dwell_seconds_by_zone == {"Z_FOH": 15.0, "Z_MAKEUP": 20.0}
    assert s.zones_visited >= {"Z_FOH", "Z_MAKEUP"}


def test_billing_queue_flags_reach_billing() -> None:
    events = [
        _ev(0, "ENTRY"),
        _ev(20, "BILLING_QUEUE_JOIN", zone_id="Z_BILLING"),
        _ev(35, "BILLING_QUEUE_ABANDON", zone_id="Z_BILLING", duration_s=15.0),
        _ev(50, "EXIT"),
    ]
    [s] = build_sessions(events)
    assert s.billing_join_count == 1
    assert s.billing_abandon_count == 1
    assert s.reached_billing is True
    assert "Z_BILLING" in s.zones_visited


def test_is_staff_stays_true_once_set() -> None:
    events = [
        _ev(0, "ENTRY", is_staff=True),
        _ev(10, "ZONE_ENTER", zone_id="Z_FOH", is_staff=False),  # detection layer wobble
        _ev(20, "EXIT", is_staff=True),
    ]
    [s] = build_sessions(events)
    assert s.is_staff is True


# ── Defensive paths ─────────────────────────────────────────────────


def test_rows_without_identity_are_skipped() -> None:
    events = [
        {**_ev(0, "ENTRY"), "person_id": None, "track_id": None},
        _ev(10, "EXIT"),
    ]
    sessions = build_sessions(events)
    # The first row is dropped; the second is for person P-1 but has no
    # opening ENTRY → no session is created.
    assert sessions == []


def test_track_id_used_when_person_id_missing() -> None:
    """Backwards-compat: pre-Re-ID rows should still session by track_id."""
    events = [
        {**_ev(0, "ENTRY"), "person_id": None},
        {**_ev(10, "EXIT"), "person_id": None},
    ]
    [s] = build_sessions(events)
    assert s.person_id == "P-1"   # falls back to track_id="P-1"


def test_multiple_people_become_separate_sessions() -> None:
    events = [
        _ev(0, "ENTRY", person="P-A"),
        _ev(30, "EXIT", person="P-A"),
        _ev(5, "ENTRY", person="P-B"),
        _ev(40, "EXIT", person="P-B"),
    ]
    sessions = build_sessions(events)
    assert len(sessions) == 2
    assert {s.person_id for s in sessions} == {"P-A", "P-B"}
