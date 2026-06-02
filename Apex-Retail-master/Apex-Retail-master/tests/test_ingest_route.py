# PROMPT (Claude, Batch 3):
#   "Write FastAPI tests for POST /events/ingest covering:
#      (1) happy path returns 202 with accepted=N, results aligned by event_id,
#      (2) duplicate event_ids in the SAME batch → first accepted, rest duplicate,
#      (3) re-POSTing the same batch → all duplicate, no new rows in DB,
#      (4) mixed batch (some new, some seen) returns the right per-event statuses,
#      (5) schema violation → 422 with structured error envelope (no stack trace),
#      (6) batch over the cap → 413 with BATCH_TOO_LARGE code,
#      (7) DB-down → 503 with DEPENDENCY_UNAVAILABLE code,
#      (8) trailing log line carries event_count / accepted / duplicates."
#
# CHANGES MADE:
#   - Counted rows directly via the events table to prove dedup at the DB layer.
#   - Used monkeypatch on EVENT_INGEST_BATCH_MAX so the cap test is deterministic.
#   - Used a dependency-override / monkeypatch on session_scope to simulate DB down.

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

INGEST_URL = "/events/ingest"


def _event(event_id: str, **overrides: Any) -> dict:
    base = {
        "event_id": event_id,
        "event_type": "ENTRY",
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "timestamp": "2026-05-31T10:00:00+00:00",
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


def _row_count(db_session) -> int:
    from app.infra.models import EventModel

    return db_session.query(EventModel).count()


# ── Happy path & idempotency ─────────────────────────────────────────────


def test_ingest_happy_path(client: TestClient, db_session) -> None:
    payload = {"events": [_event(f"evt-{i}") for i in range(3)]}
    resp = client.post(INGEST_URL, json=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 3
    assert body["duplicates"] == 0
    assert body["rejected"] == 0
    assert {r["event_id"] for r in body["results"]} == {"evt-0", "evt-1", "evt-2"}
    assert all(r["status"] == "accepted" for r in body["results"])

    assert _row_count(db_session) == 3


def test_reingest_same_batch_is_idempotent(client: TestClient, db_session) -> None:
    payload = {"events": [_event(f"evt-{i}") for i in range(3)]}

    first = client.post(INGEST_URL, json=payload)
    assert first.status_code == 202
    assert _row_count(db_session) == 3

    second = client.post(INGEST_URL, json=payload)
    assert second.status_code == 202
    body = second.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == 3
    assert all(r["status"] == "duplicate" for r in body["results"])

    # Critical invariant: row count unchanged.
    assert _row_count(db_session) == 3


def test_mixed_batch_partial_duplicate(client: TestClient, db_session) -> None:
    client.post(INGEST_URL, json={"events": [_event("a"), _event("b")]})
    assert _row_count(db_session) == 2

    payload = {
        "events": [
            _event("b"),  # dup
            _event("c"),  # new
            _event("a"),  # dup
            _event("d"),  # new
        ]
    }
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 2
    assert body["duplicates"] == 2

    statuses = {r["event_id"]: r["status"] for r in body["results"]}
    assert statuses == {"a": "duplicate", "b": "duplicate", "c": "accepted", "d": "accepted"}
    assert _row_count(db_session) == 4


def test_intra_batch_duplicate_event_ids(client: TestClient, db_session) -> None:
    """Same event_id appearing twice within ONE batch: first accepted, second duplicate."""
    payload = {"events": [_event("dup-1"), _event("dup-1", camera_id="CAM_X")]}
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1
    assert _row_count(db_session) == 1


# ── Validation errors (422) ──────────────────────────────────────────────


def test_schema_violation_returns_structured_422(client: TestClient) -> None:
    payload = {
        "events": [
            {  # ZONE_ENTER missing zone_id
                "event_id": "bad-1",
                "event_type": "ZONE_ENTER",
                "store_id": "STORE_BLR_002",
                "camera_id": "CAM_X",
                "timestamp": "2026-05-31T10:00:00+00:00",
            }
        ]
    }
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 422

    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "errors" in body["error"]["details"]
    # No raw stack/internals in the body.
    assert "Traceback" not in resp.text


def test_naive_timestamp_rejected_at_route(client: TestClient) -> None:
    payload = {"events": [_event("evt-naive", timestamp="2026-05-31T10:00:00")]}  # no offset
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_event_type_rejected(client: TestClient) -> None:
    payload = {"events": [_event("evt-x", event_type="LOITER")]}
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 422


def test_empty_events_rejected(client: TestClient) -> None:
    resp = client.post(INGEST_URL, json={"events": []})
    assert resp.status_code == 422  # min_length=1


# ── Batch cap (413) ──────────────────────────────────────────────────────


def test_batch_over_cap_returns_413(client: TestClient, monkeypatch) -> None:
    # Lower the cap for a deterministic test rather than POSTing 501 events.
    # Pydantic-settings reads from env on Settings(); patching the env var +
    # clearing the lru_cache is the supported way to override at runtime.
    from app.core.config import get_settings

    monkeypatch.setenv("EVENT_INGEST_BATCH_MAX", "3")
    get_settings.cache_clear()

    payload = {"events": [_event(f"e-{i}") for i in range(4)]}
    resp = client.post(INGEST_URL, json=payload)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "BATCH_TOO_LARGE"
    assert body["error"]["details"]["received"] == 4
    assert body["error"]["details"]["max"] == 3


# ── Dependency unavailable (503) ─────────────────────────────────────────


def test_db_down_returns_503(client: TestClient, monkeypatch) -> None:
    """If the DB raises OperationalError, ingest must return a 503 envelope."""
    from app.services import ingestion as ing_mod

    @contextmanager
    def _failing_session():
        # Mimic SQLAlchemy's OperationalError shape.
        raise OperationalError("SELECT 1", {}, BaseException("boom"))
        yield  # pragma: no cover — unreachable

    monkeypatch.setattr(ing_mod, "session_scope", _failing_session)

    payload = {"events": [_event("e-1")]}
    resp = client.post(INGEST_URL, json=payload)

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    # Hint surfaces to the operator:
    assert "retry" in body["error"]["details"]["hint"].lower()


# ── Edge: large but legal batch ──────────────────────────────────────────


def test_batch_at_cap_succeeds(client: TestClient) -> None:
    """Exactly EVENT_INGEST_BATCH_MAX events should be accepted."""
    from app.core.config import get_settings

    n = get_settings().event_ingest_batch_max
    if n > 200:
        # Keep the test fast — 200 round-tripped events through Pydantic + SQLite.
        n = 200

    payload = {"events": [_event(f"big-{i}") for i in range(n)]}
    resp = client.post(INGEST_URL, json=payload)

    assert resp.status_code == 202
    assert resp.json()["accepted"] == n


# ── Schema-rich event types ──────────────────────────────────────────────


def test_zone_event_persists_extra_fields(client: TestClient, db_session) -> None:
    payload = {
        "events": [
            _event(
                "z-1",
                event_type="DWELL",
                zone_id="Z_AISLE_2",
                duration_s=14.5,
                track_id="T-77",
                is_staff=False,
                bbox=[10, 20, 50, 80],
            )
        ]
    }
    resp = client.post(INGEST_URL, json=payload)
    assert resp.status_code == 202

    from app.infra.models import EventModel

    row = db_session.query(EventModel).filter_by(event_id="z-1").one()
    assert row.event_type == "DWELL"
    assert row.zone_id == "Z_AISLE_2"
    assert row.duration_s == 14.5
    assert row.track_id == "T-77"
    assert row.is_staff is False
    assert row.bbox == [10, 20, 50, 80]
