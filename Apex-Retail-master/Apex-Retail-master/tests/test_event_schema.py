# PROMPT (Claude, Batch 2):
#   "Write Pydantic v2 schema tests covering: closed-set event_type, UTC tz
#    enforcement, zone_id required/forbidden by event_type, duration required
#    for DWELL/BILLING_*, bbox shape, confidence range, and round-trip JSON."
#
# CHANGES MADE:
#   - Added a roundtrip test using model_dump_json + model_validate_json to
#     catch any future drift in serialisers.
#   - Asserted that extra unknown fields land in `payload` (forward-compat).

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.events import EventType
from app.schemas.events import Event


def _base(**overrides):
    """Minimal valid ENTRY event."""
    base = {
        "event_id": "evt-1",
        "event_type": "ENTRY",
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "timestamp": "2026-05-31T10:00:00+00:00",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_minimal_entry_event_validates() -> None:
    ev = Event.model_validate(_base())
    assert ev.event_type == EventType.ENTRY
    assert ev.timestamp.tzinfo is timezone.utc


def test_unknown_event_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_type="WALKING_AROUND"))


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(timestamp="2026-05-31T10:00:00"))  # no offset


def test_zone_required_for_zone_enter() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_type="ZONE_ENTER"))  # missing zone_id

    ev = Event.model_validate(_base(event_type="ZONE_ENTER", zone_id="Z_BILLING"))
    assert ev.zone_id == "Z_BILLING"


def test_zone_forbidden_on_entry() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(zone_id="Z_ANY"))  # ENTRY + zone_id


def test_duration_required_for_dwell_and_queue() -> None:
    for et in ("DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
        with pytest.raises(ValidationError):
            Event.model_validate(_base(event_type=et, zone_id="Z_X"))
        ev = Event.model_validate(_base(event_type=et, zone_id="Z_X", duration_s=12.5))
        assert ev.duration_s == 12.5


def test_bbox_shape_validated() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(bbox=[10, 20, 5, 5]))  # x2 < x1
    with pytest.raises(ValidationError):
        Event.model_validate(_base(bbox=[10, 20, 30]))  # only 3 numbers
    ev = Event.model_validate(_base(bbox=[10, 20, 30, 50]))
    assert ev.bbox == (10.0, 20.0, 30.0, 50.0)


def test_confidence_range() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_base(confidence=1.2))
    with pytest.raises(ValidationError):
        Event.model_validate(_base(confidence=-0.01))


def test_extra_fields_kept_in_payload() -> None:
    ev = Event.model_validate(_base(some_future_field="abc", another=42))
    assert ev.payload["some_future_field"] == "abc"
    assert ev.payload["another"] == 42


def test_roundtrip_json() -> None:
    ev = Event.model_validate(
        _base(event_type="DWELL", zone_id="Z_AISLE_3", duration_s=22.0)
    )
    blob = ev.model_dump_json()
    ev2 = Event.model_validate_json(blob)
    assert ev == ev2

    # And the JSON is still parseable as plain JSON (no exotic types leak).
    parsed = json.loads(blob)
    assert parsed["event_type"] == "DWELL"
    assert parsed["zone_id"] == "Z_AISLE_3"


def test_timestamp_z_suffix_accepted() -> None:
    ev = Event.model_validate(_base(timestamp="2026-05-31T10:00:00Z"))
    assert ev.timestamp.tzinfo is timezone.utc
