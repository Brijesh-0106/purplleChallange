# PROMPT (Claude, Batch 1, updated Batch 2 + Batch 7):
#   "Tests for /health, root, sanitised 500, structured 404 — and (Batch 2)
#    a dependencies array + (Batch 7) per-store stale-feed reporting."
#
# CHANGES MADE (B7):
#   - Added 3 stale-feed tests:
#       * Empty DB → no `stores` array (or empty); status remains 'ok'.
#       * Recent event → store appears with status='ok', stale=False.
#       * Old event > threshold → status='stale_feed' AND overall=='degraded'.

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$")


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] in {"ok", "degraded"}  # SQLite test DB → ok
    assert body["service"] == "apex-retail-store-intelligence"
    assert isinstance(body["version"], str) and body["version"]
    assert body["environment"] in {"local", "dev", "prod"}
    assert ISO_UTC_RE.match(body["timestamp"]), body["timestamp"]

    assert isinstance(body["dependencies"], list)
    assert any(d["name"] == "postgres" for d in body["dependencies"])
    assert "stores" in body and isinstance(body["stores"], list)


def test_root_returns_pointers(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200

    body = resp.json()
    assert body["health"] == "/health"
    assert body["docs"] == "/docs"


def test_unknown_route_returns_structured_404(client: TestClient) -> None:
    resp = client.get("/this-does-not-exist")
    assert resp.status_code == 404

    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "HTTP_404"


def test_unhandled_exception_is_sanitised() -> None:
    """A raw exception in a route must NEVER leak a stack trace to the client."""
    from app.main import create_app

    app = create_app()

    @app.get("/__boom__", include_in_schema=False)
    async def _boom() -> None:
        raise RuntimeError("internal detail that must not be exposed")

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/__boom__")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in resp.text


# ── Batch 7 — stale-feed reporting ──────────────────────────────────


def _seed_event_for(db_session, *, store_id: str, ts: datetime) -> None:
    from app.infra.models import EventModel

    db_session.add(
        EventModel(
            event_id=f"hb-{store_id}-{int(ts.timestamp())}",
            event_type="ENTRY",
            store_id=store_id,
            camera_id="CAM_X",
            timestamp=ts,
            track_id="T-1",
            person_id="P-1",
            confidence=0.9,
            is_staff=False,
            received_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()


def test_health_recent_event_marks_store_ok(client: TestClient, db_session) -> None:
    now = datetime.now(timezone.utc)
    _seed_event_for(db_session, store_id="ST1008", ts=now - timedelta(seconds=30))

    resp = client.get("/health")
    body = resp.json()
    by_id = {s["store_id"]: s for s in body["stores"]}
    assert "ST1008" in by_id
    assert by_id["ST1008"]["stale"] is False
    assert by_id["ST1008"]["status"] == "ok"
    # Overall status remains ok (postgres up, no stale stores).
    assert body["status"] == "ok"


def test_health_old_event_flagged_stale_and_degraded(client: TestClient, db_session) -> None:
    """Last event > threshold (default 10 min) → STALE_FEED + degraded."""
    now = datetime.now(timezone.utc)
    _seed_event_for(db_session, store_id="ST1008", ts=now - timedelta(minutes=30))

    resp = client.get("/health")
    body = resp.json()
    by_id = {s["store_id"]: s for s in body["stores"]}
    assert by_id["ST1008"]["stale"] is True
    assert by_id["ST1008"]["status"] == "stale_feed"
    assert body["status"] == "degraded"


def test_health_with_no_events_has_empty_stores_list(client: TestClient) -> None:
    resp = client.get("/health")
    body = resp.json()
    assert body["stores"] == []
    assert body["status"] == "ok"
