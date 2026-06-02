# PROMPT (Claude, Batch 8):
#   "Test the Broadcaster: (1) subscribe/unsubscribe updates count,
#    (2) publish reaches subscribers and serialises payload as JSON,
#    (3) failed sends are reaped silently (broadcaster never raises),
#    (4) only subscribers of the same store_id receive."
#
# CHANGES MADE:
#   - Used a tiny FakeWebSocket that records sent_text + supports a
#     'closed' state, instead of pulling in the full Starlette test client
#     (the unit test only cares about Broadcaster behaviour).

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.websockets import WebSocketState

from app.services.broadcaster import Broadcaster


class FakeWebSocket:
    """Minimal stand-in for `fastapi.WebSocket` for broadcaster unit tests."""

    def __init__(self, *, raise_on_send: bool = False, closed: bool = False) -> None:
        self.client_state = WebSocketState.DISCONNECTED if closed else WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self._raise = raise_on_send

    async def send_text(self, body: str) -> None:
        if self._raise:
            raise ConnectionError("simulated socket failure")
        self.sent_text.append(body)


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe_track_count() -> None:
    br = Broadcaster()
    a = FakeWebSocket()
    b = FakeWebSocket()
    await br.subscribe("ST1008", a)
    await br.subscribe("ST1008", b)
    assert br.subscriber_count("ST1008") == 2
    await br.unsubscribe("ST1008", a)
    assert br.subscriber_count("ST1008") == 1


@pytest.mark.asyncio
async def test_publish_reaches_subscribers_as_json() -> None:
    br = Broadcaster()
    a = FakeWebSocket()
    await br.subscribe("ST1008", a)
    await br.publish("ST1008", {"type": "store_changed", "n": 5})

    assert len(a.sent_text) == 1
    body = json.loads(a.sent_text[0])
    assert body == {"type": "store_changed", "n": 5}


@pytest.mark.asyncio
async def test_publish_only_to_subscribed_store() -> None:
    br = Broadcaster()
    a = FakeWebSocket()
    b = FakeWebSocket()
    await br.subscribe("ST1008", a)
    await br.subscribe("ST9999", b)
    await br.publish("ST1008", {"hello": True})

    assert a.sent_text == ['{"hello": true}']
    assert b.sent_text == []


@pytest.mark.asyncio
async def test_failed_send_is_reaped_silently() -> None:
    br = Broadcaster()
    bad = FakeWebSocket(raise_on_send=True)
    good = FakeWebSocket()
    await br.subscribe("ST1008", bad)
    await br.subscribe("ST1008", good)

    # Should not raise even though `bad.send_text` does.
    await br.publish("ST1008", {"event_count": 1})

    # The good client got the message; the bad client was reaped.
    assert good.sent_text == ['{"event_count": 1}']
    assert br.subscriber_count("ST1008") == 1


@pytest.mark.asyncio
async def test_disconnected_client_is_reaped() -> None:
    br = Broadcaster()
    closed = FakeWebSocket(closed=True)
    await br.subscribe("ST1008", closed)
    await br.publish("ST1008", {"x": 1})
    assert closed.sent_text == []
    assert br.subscriber_count("ST1008") == 0


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop() -> None:
    br = Broadcaster()
    # Should simply return without raising.
    await br.publish("ST1008", {"x": 1})


def test_get_broadcaster_returns_singleton() -> None:
    from app.services.broadcaster import get_broadcaster

    a = get_broadcaster()
    b = get_broadcaster()
    assert a is b


# ── Allow asyncio fixtures without explicit `@pytest.fixture(...)` ──
# `pytest-asyncio` is in requirements.txt; the conftest sets asyncio_mode=auto
# in pyproject.toml so the @pytest.mark.asyncio above works out of the box.
