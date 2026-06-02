"""Health endpoint.

Layered escalation across batches:
    Batch 1 — minimal liveness + version.
    Batch 2 — DB connectivity probe (graceful degradation).
    Batch 7 — per-store last-event timestamps + `STALE_FEED` flag when a
              store hasn't emitted events in `STALE_FEED_THRESHOLD_MINUTES`.

The STALE_FEED warning is exactly the signal an SRE wants on /health: a
store whose detection pipeline has fallen over WITHOUT crashing the API
itself. We surface it as `degraded` so an external monitor can page on
status≠ok without paging on a fully-down API (status="down" / non-2xx).

Empty event store → no per-store entries are emitted (we don't synthesise
"unknown" rows). The dependencies array still carries the postgres ping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.infra.db import get_session_factory
from app.infra.db import ping as db_ping
from app.infra.models import EventModel

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────
# Response schemas
# ──────────────────────────────────────────────────────────────────


class DependencyStatus(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_at: datetime | None
    lag_seconds: float | None
    stale: bool
    status: Literal["ok", "stale_feed", "no_data"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"] = "ok"
    service: str
    version: str
    environment: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    dependencies: list[DependencyStatus] = Field(default_factory=list)
    stores: list[StoreFeedStatus] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse, summary="Liveness + readiness probe")
async def health() -> HealthResponse:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=settings.stale_feed_threshold_minutes)

    db_ok = db_ping()
    deps = [
        DependencyStatus(
            name="postgres", healthy=db_ok, detail=None if db_ok else "ping failed"
        )
    ]

    stores: list[StoreFeedStatus] = []
    overall: Literal["ok", "degraded", "down"] = "ok"

    if db_ok:
        try:
            stores = _per_store_feed_status(now=now, threshold=threshold)
        except Exception as exc:  # noqa: BLE001 — health must absorb everything
            logger.warning("health.per_store_failed", error=str(exc))
            stores = []

    if not db_ok:
        overall = "degraded"
    elif any(s.status == "stale_feed" for s in stores):
        # Even one store with a stale feed is operationally important —
        # `degraded` is the right signal for an external monitor.
        overall = "degraded"

    return HealthResponse(
        status=overall,
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        timestamp=now,
        dependencies=deps,
        stores=stores,
    )


# ──────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────


def _per_store_feed_status(
    *, now: datetime, threshold: timedelta
) -> list[StoreFeedStatus]:
    """For every store with at least one event, report the most recent
    event's timestamp and a STALE_FEED flag when the lag exceeds
    `threshold`. Stores with zero events are NOT included — there's nothing
    operationally actionable to say about a store we've never seen.
    """
    factory = get_session_factory()
    with factory() as sess:
        rows = sess.execute(
            select(
                EventModel.store_id, func.max(EventModel.timestamp).label("last_ts")
            )
            .group_by(EventModel.store_id)
            .order_by(EventModel.store_id)
        ).all()

    out: list[StoreFeedStatus] = []
    for store_id, last_ts in rows:
        if last_ts is None:
            out.append(
                StoreFeedStatus(
                    store_id=store_id,
                    last_event_at=None,
                    lag_seconds=None,
                    stale=False,
                    status="no_data",
                )
            )
            continue
        # SQLite drops tz info on round-trip; tag UTC so subtraction works.
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        lag = now - last_ts
        stale = lag > threshold
        out.append(
            StoreFeedStatus(
                store_id=store_id,
                last_event_at=last_ts,
                lag_seconds=round(lag.total_seconds(), 2),
                stale=stale,
                status="stale_feed" if stale else "ok",
            )
        )
    return out