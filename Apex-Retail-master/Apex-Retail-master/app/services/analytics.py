"""Top-level analytics use-case — orchestrates the moving parts.

Routes call `AnalyticsService.metrics(store_id)` (or `.funnel`, `.heatmap`)
and get a finished report. The service owns DB session lifecycle, fetches
events + POS rows, builds sessions, runs POS correlation, and finally
delegates to the pure compute_* functions.

This is what keeps the routes thin: they're just HTTP DTO mapping +
status-code logic, not business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import get_settings
from app.core.errors import DependencyUnavailableError
from app.core.logging import get_logger
from app.infra.db import session_scope
from app.infra.repositories.events_repo import EventRepository
from app.infra.repositories.pos_repo import POSRepository
from app.services.funnel import FunnelReport, compute_funnel
from app.services.heatmap import HeatmapReport, compute_heatmap
from app.services.metrics import StoreMetrics, compute_store_metrics
from app.services.pos_correlation import correlate_purchases
from app.services.sessions import Session, build_sessions

logger = get_logger(__name__)


@dataclass(slots=True)
class _LoadedData:
    sessions: list[Session]
    pos_rows: list[dict]
    earliest_event_ts: datetime | None
    latest_event_ts: datetime | None


class AnalyticsService:
    """Single entrypoint for the /metrics, /funnel, /heatmap routes."""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ── Public API ───────────────────────────────────────────────────

    def metrics(
        self,
        store_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> StoreMetrics:
        loaded = self._load(store_id, since=since, until=until)
        # Use the actual event range as the window when no range was requested
        # — that matches "metrics over the data we actually have".
        win_start = since or loaded.earliest_event_ts
        win_end = until or loaded.latest_event_ts
        return compute_store_metrics(
            loaded.sessions,
            store_id=store_id,
            window_start=win_start,
            window_end=win_end,
        )

    def funnel(
        self,
        store_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> FunnelReport:
        loaded = self._load(store_id, since=since, until=until)
        return compute_funnel(loaded.sessions, store_id=store_id)

    def heatmap(
        self,
        store_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HeatmapReport:
        loaded = self._load(store_id, since=since, until=until)
        return compute_heatmap(loaded.sessions, store_id=store_id)

    # ── Internal load + sessionise + correlate pipeline ──────────────

    def _load(
        self,
        store_id: str,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> _LoadedData:
        try:
            with session_scope() as db:
                ev_repo = EventRepository(db)
                pos_repo = POSRepository(db)

                events = ev_repo.fetch_for_analytics(store_id, since=since, until=until)
                pos_rows = self._fetch_pos(pos_repo, store_id, events)
        except OperationalError as exc:
            logger.error("analytics.db_unavailable", error=str(exc))
            raise DependencyUnavailableError(
                "Database is unavailable; analytics cannot be computed."
            ) from exc
        except SQLAlchemyError:
            logger.exception("analytics.db_error")
            raise

        sessions = build_sessions(
            events, session_gap_minutes=self.settings.session_gap_minutes
        )
        sessions = correlate_purchases(
            sessions,
            pos_rows,
            window_minutes=self.settings.pos_correlation_window_minutes,
        )

        earliest = events[0]["timestamp"] if events else None
        # `events` is sorted by (person_id, ts), not by ts globally, so we
        # compute earliest/latest with min/max rather than head/tail.
        if events:
            timestamps = [e["timestamp"] for e in events]
            earliest = min(timestamps)
            latest = max(timestamps)
        else:
            latest = None

        return _LoadedData(
            sessions=sessions,
            pos_rows=pos_rows,
            earliest_event_ts=earliest,
            latest_event_ts=latest,
        )

    @staticmethod
    def _fetch_pos(
        pos_repo: POSRepository, store_id: str, events: Iterable[dict]
    ) -> list[dict]:
        """Pull POS rows that overlap the event time range.

        We fetch a slightly-wider window than the events span to catch POS
        rows that landed just before the first event or just after the last.
        """
        ts_list = [e["timestamp"] for e in events]
        if not ts_list:
            return []
        # Fetch a wide window so any session that bills near the edges still
        # gets correlated. Cheap on a single store-day.
        from datetime import timedelta

        lo = min(ts_list) - timedelta(hours=1)
        hi = max(ts_list) + timedelta(hours=1)
        return pos_repo.fetch_in_window(store_id, lo, hi)
