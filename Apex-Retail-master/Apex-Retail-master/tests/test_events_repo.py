# PROMPT (Claude, Batch 2):
#   "Write tests for EventRepository.insert_many that prove: (1) the happy
#    path inserts everything, (2) re-inserting the same payload classifies
#    them as duplicates with zero new rows, (3) a mixed batch (some new,
#    some duplicate) returns the right per-event statuses."
#
# CHANGES MADE:
#   - Added a count-based invariant after each insert to catch silent dupes.
#   - Used a parametrised "make_event" helper to keep test bodies readable.

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.events import EventType
from app.infra.repositories.events_repo import EventRepository
from app.schemas.events import Event


def make_event(event_id: str, **overrides) -> Event:
    base = dict(
        event_id=event_id,
        event_type="ENTRY",
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        timestamp=datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc),
        confidence=0.95,
    )
    base.update(overrides)
    return Event.model_validate(base)


def test_insert_batch_happy_path(db_session) -> None:
    repo = EventRepository(db_session)
    events = [make_event(f"evt-{i}") for i in range(5)]

    outcomes = repo.insert_many(events)
    db_session.commit()

    assert [o.status for o in outcomes] == ["accepted"] * 5
    assert repo.count_for_store("STORE_BLR_002") == 5


def test_insert_is_idempotent(db_session) -> None:
    repo = EventRepository(db_session)
    events = [make_event(f"evt-{i}") for i in range(3)]

    repo.insert_many(events)
    db_session.commit()

    again = repo.insert_many(events)
    db_session.commit()

    assert [o.status for o in again] == ["duplicate"] * 3
    assert repo.count_for_store("STORE_BLR_002") == 3  # no new rows


def test_mixed_batch_partial_duplicate(db_session) -> None:
    repo = EventRepository(db_session)

    repo.insert_many([make_event("e-1"), make_event("e-2")])
    db_session.commit()

    mixed = [
        make_event("e-2"),  # dup
        make_event("e-3"),  # new
        make_event("e-1"),  # dup
        make_event("e-4"),  # new
    ]
    outcomes = repo.insert_many(mixed)
    db_session.commit()

    statuses = {o.event_id: o.status for o in outcomes}
    assert statuses == {
        "e-2": "duplicate",
        "e-3": "accepted",
        "e-1": "duplicate",
        "e-4": "accepted",
    }
    assert repo.count_for_store("STORE_BLR_002") == 4


def test_empty_batch_is_noop(db_session) -> None:
    repo = EventRepository(db_session)
    assert repo.insert_many([]) == []


def test_latest_timestamp_tracked(db_session) -> None:
    repo = EventRepository(db_session)
    later = datetime(2026, 5, 31, 11, 0, 0, tzinfo=timezone.utc)
    repo.insert_many(
        [
            make_event("a"),
            make_event("b", timestamp=later),
        ]
    )
    db_session.commit()

    latest = repo.latest_timestamp("STORE_BLR_002")
    assert latest is not None
    # SQLite drops tz on round-trip; compare on naive parts to stay portable.
    assert latest.replace(tzinfo=None) == later.replace(tzinfo=None)


@pytest.mark.parametrize(
    "event_type, extras",
    [
        ("ZONE_ENTER", {"zone_id": "Z_AISLE_2"}),
        ("DWELL", {"zone_id": "Z_AISLE_2", "duration_s": 30.0}),
        ("BILLING_QUEUE_JOIN", {"zone_id": "Z_BILLING", "duration_s": 0.0}),
    ],
)
def test_repo_persists_optional_fields(db_session, event_type, extras) -> None:
    repo = EventRepository(db_session)
    ev = make_event("zone-evt", event_type=event_type, **extras)
    repo.insert_many([ev])
    db_session.commit()

    from app.infra.models import EventModel

    row = db_session.query(EventModel).filter_by(event_id="zone-evt").one()
    assert row.event_type == event_type
    if "zone_id" in extras:
        assert row.zone_id == extras["zone_id"]
    if "duration_s" in extras:
        assert row.duration_s == extras["duration_s"]
