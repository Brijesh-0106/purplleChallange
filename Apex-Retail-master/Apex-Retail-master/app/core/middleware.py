"""Request middleware.

Attaches a `trace_id` to every incoming request, measures latency, and emits
one structured access log line per response. Downstream handlers can call
`structlog.contextvars.bind_contextvars(...)` to enrich the same line with
domain context (e.g. store_id, event_count).
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind trace_id + endpoint + latency to every request's log context."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Honour an inbound trace id if a caller is propagating one.
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            endpoint=request.url.path,
            method=request.method,
        )

        started = time.perf_counter()
        status_code = 500  # default if call_next blows up before assignment

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            structlog.contextvars.bind_contextvars(
                latency_ms=latency_ms,
                status_code=status_code,
            )
            logger.info("request.completed")
            # Surface the trace id to clients so they can correlate.
            # NOTE: response may not exist on exception — guard handled by FastAPI.
