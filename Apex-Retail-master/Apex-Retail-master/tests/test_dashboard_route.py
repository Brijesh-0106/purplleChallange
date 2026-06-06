# PROMPT (Claude, Batch 8):
#   "Test the dashboard endpoints: (1) GET /dashboard returns HTML with the
#    Chart.js script tag, (2) the WebSocket /ws/stores/{id} accepts a
#    connection, (3) a publish to that store reaches the connected client."
#
# CHANGES MADE:
#   - Verified HTML response containing Chart.js references.
#   - Verified WebSocket connection and pub/sub message delivery using FastAPI's TestClient and Broadcaster.

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_dashboard_serves_html(client: TestClient) -> None:
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "<title>Apex Retail" in body
    assert "chart.js" in body.lower()


def test_websocket_accepts_connection_and_receives_publish() -> None:
    """Use the broadcaster directly; a publish during an active connection
    should reach the client."""
    import asyncio

    from app.main import create_app
    from app.services.broadcaster import get_broadcaster

    app = create_app()
    with TestClient(app) as c:
        with c.websocket_connect("/ws/stores/ST1008") as ws:
            # Give FastAPI a tick to register the subscriber before publishing.
            br = get_broadcaster()

            # Run the publish on the test's running event loop.
            async def _publish():
                # Wait until the subscriber count flips to 1 (with a tiny budget).
                for _ in range(50):
                    if br.subscriber_count("ST1008") >= 1:
                        break
                    await asyncio.sleep(0.01)
                await br.publish("ST1008", {"type": "store_changed", "store_id": "ST1008"})

            asyncio.get_event_loop().run_until_complete(_publish())

            received = ws.receive_text()
            payload = json.loads(received)
            assert payload["type"] == "store_changed"
            assert payload["store_id"] == "ST1008"
