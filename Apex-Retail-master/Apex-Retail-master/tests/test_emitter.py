# PROMPT (Claude, Batch 4):
#   "Write tests for emitter: (1) DryRunEmitter collects in-memory and
#    can print, (2) HttpEmitter.emit batches and POSTs, (3) HttpEmitter
#    re-queues events on persistent network failure, (4) HttpEmitter
#    retries on 503, (5) HttpEmitter does NOT retry on 422."
#
# CHANGES MADE:
#   - Used httpx.MockTransport rather than monkeypatching `requests` —
#     httpx's first-class mock harness gives realistic responses without
#     a real socket.
#   - Patched time.sleep to keep retry tests fast.

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.schemas.events import Event
from pipeline.emitter import DryRunEmitter, HttpEmitter


def _evt(event_id: str = "evt-1") -> Event:
    return Event.model_validate(
        {
            "event_id": event_id,
            "event_type": "ENTRY",
            "store_id": "STORE_DEMO_001",
            "camera_id": "CAM_FLOOR_01",
            "timestamp": "2026-06-01T10:00:00+00:00",
            "confidence": 0.9,
        }
    )


# ── DryRun ───────────────────────────────────────────────────────────


def test_dry_run_collects_events() -> None:
    em = DryRunEmitter()
    em.emit([_evt("a"), _evt("b")])
    em.emit([_evt("c")])
    em.flush()
    assert [e.event_id for e in em.collected] == ["a", "b", "c"]


def test_dry_run_print_to_stdout(capsys) -> None:
    em = DryRunEmitter(print_to_stdout=True)
    em.emit([_evt("a")])
    captured = capsys.readouterr()
    assert "evt-1" in captured.out or '"event_id":"a"' in captured.out


# ── HTTP happy path ──────────────────────────────────────────────────


def _mount_transport(em: HttpEmitter, transport: httpx.MockTransport) -> None:
    """Attach a mocked transport to the emitter's lazily-created client."""
    em._client = httpx.Client(transport=transport, timeout=em.timeout_s)


def test_http_emit_posts_and_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.URL(str(request.url))
        seen.append({"path": body.path, "json": _json(request)})
        return httpx.Response(
            202, json={"accepted": 1, "duplicates": 0, "rejected": 0, "results": []}
        )

    em = HttpEmitter(base_url="http://api.local", batch_size=10)
    _mount_transport(em, httpx.MockTransport(handler))

    em.emit([_evt("a")])
    em.flush()

    assert len(seen) == 1
    assert seen[0]["path"] == "/events/ingest"
    assert "events" in seen[0]["json"]


def test_http_batches_at_threshold(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(_json(request)["events"]))
        return httpx.Response(202, json={"accepted": 0, "duplicates": 0, "rejected": 0, "results": []})

    em = HttpEmitter(base_url="http://api.local", batch_size=2)
    _mount_transport(em, httpx.MockTransport(handler))

    em.emit([_evt(f"e{i}") for i in range(5)])
    em.flush()

    assert sum(calls) == 5
    # First two POSTs should have batch=2, final flush=1
    assert calls[:2] == [2, 2]
    assert calls[-1] == 1


# ── Retry / failure paths ────────────────────────────────────────────


def test_http_retries_on_503(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    attempts = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"error": {"code": "DEPENDENCY_UNAVAILABLE"}})
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "rejected": 0, "results": []})

    em = HttpEmitter(base_url="http://api.local", batch_size=1, max_retries=5)
    _mount_transport(em, httpx.MockTransport(handler))

    em.emit([_evt("a")])
    em.flush()
    assert attempts["n"] == 3
    assert em.pending() == []


def test_http_requeues_on_persistent_network_failure(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    em = HttpEmitter(base_url="http://api.local", batch_size=1, max_retries=2)
    _mount_transport(em, httpx.MockTransport(handler))

    em.emit([_evt("a"), _evt("b")])
    em.flush()
    # Both events should still be queued after the emitter exhausted retries.
    assert {e.event_id for e in em.pending()} == {"a", "b"}


def test_http_does_not_retry_on_422(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    attempts = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(422, json={"error": {"code": "VALIDATION_ERROR"}})

    em = HttpEmitter(base_url="http://api.local", batch_size=1, max_retries=5)
    _mount_transport(em, httpx.MockTransport(handler))

    em.emit([_evt("a")])
    em.flush()
    # Only one attempt — 422 is a client-side error, retrying would never help.
    assert attempts["n"] == 1
    # And the offending batch is re-queued so the operator can inspect.
    assert [e.event_id for e in em.pending()] == ["a"]


# ── Helpers ──────────────────────────────────────────────────────────


def _json(req: httpx.Request) -> dict:
    import json

    return json.loads(req.content.decode("utf-8"))
