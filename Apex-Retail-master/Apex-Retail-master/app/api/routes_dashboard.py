"""Dashboard routes.

A single-page live dashboard at GET `/dashboard`, plus the WebSocket upgrade
endpoint at `/ws/stores/{store_id}` it connects to. Both are mounted on the
main FastAPI app (Batch 8).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.core.logging import get_logger
from app.services.broadcaster import get_broadcaster

logger = get_logger(__name__)
router = APIRouter(tags=["dashboard"])

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_INDEX_HTML = _STATIC_DIR / "dashboard.html"


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Live store-intelligence dashboard.",
)
async def dashboard() -> HTMLResponse:
    """Serve the static dashboard HTML.

    Kept as a raw HTML response (rather than a `StaticFiles` mount) so the
    page is reachable from `/dashboard` without trailing slashes or extra
    routing ceremony, and so the file is always read fresh in dev.
    """
    if not _INDEX_HTML.exists():
        return HTMLResponse(
            "<h1>Dashboard not found</h1>"
            "<p>app/static/dashboard.html is missing.</p>",
            status_code=500,
        )
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@router.websocket("/ws/stores/{store_id}")
async def stream_store(ws: WebSocket, store_id: str) -> None:
    """WebSocket endpoint that streams `store_changed` pings for a store.

    The client subscribes once and then issues HTTP fetches against the
    analytics endpoints whenever a ping arrives. Two-channel design (push
    pings, pull data) keeps WebSocket payloads tiny while letting the
    dashboard refresh as fast as the API allows.
    """
    await ws.accept()
    br = get_broadcaster()
    await br.subscribe(store_id, ws)
    try:
        # Idle loop — the broadcaster pushes; we just need to keep the
        # connection alive and notice when it closes.
        while True:
            # `receive_text` raises WebSocketDisconnect on close; otherwise
            # we ignore client-sent text. (Future: heartbeat / pong here.)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — log + drop the connection
        logger.warning("ws.stream_error", error=str(exc), store_id=store_id)
    finally:
        await br.unsubscribe(store_id, ws)
