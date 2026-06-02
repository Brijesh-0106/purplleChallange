"""Anomaly service — orchestrates the rules and feeds them real data.

Mirrors `AnalyticsService` (Batch 6): pulls events + sessions + POS via the
repositories, runs each rule, sorts and returns. Routes call this once and
get a finished anomaly list.

Concretely.
    1. Resolve the analysis window. By default we look at the most recent
       1 hour of events; the route can override via `since`/`until`.
    2. Build sessions for the current window (POS-correlated).
    3. Build a baseline window (default: 24 h ending at `since`) so the
       conversion-drop rule has something to compare against.
    4. Pull the store's expected zones from the layout repository (falls
       back to the in-code Brigade Road default — the demo store).
    5. Run all three rules; concat results; sort by severity then time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import get_settings
from app.core.errors import DependencyUnavailableError
from app.core.logging import get_logger
from app.domain.anomaly import Anomaly, Severity
from app.infra.db import session_scope
from app.infra.repositories.events_repo import EventRepository
from app.infra.repositories.pos_repo import POSRepository
from app.services.anomaly_rules import (
    detect_conversion_drop,
    detect_dead_zone,
    detect_queue_spike,
)
from app.services.pos_correlation import correlate_purchases
from app.services.sessions import build_sessions

logger = get_logger(__name__)


_DEFAULT_CURRENT_WINDOW = timedelta(hours=1)
_DEFAULT_BASELINE_LOOKBACK = timedelta(hours=24)


# Severity ordering: critical first.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.WARN: 1,
    Severity.INFO: 2,
}


class AnomalyService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def list_for_store(
        self,
        store_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        now: datetime | None = None,
    ) -> list[Anomaly]:
        now = now or datetime.now(tz=timezone.utc)
        cur_until = until or now
        cur_since = since or (cur_until - _DEFAULT_CURRENT_WINDOW)
        base_until = cur_since
        base_since = base_until - _DEFAULT_BASELINE_LOOKBACK

        try:
            with session_scope() as db:
                ev_repo = EventRepository(db)
                pos_repo = POSRepository(db)

                # Current-window events / sessions.
                cur_events = ev_repo.fetch_for_analytics(
                    store_id, since=cur_since, until=cur_until
                )
                cur_sessions = self._sessionise(cur_events, pos_repo, store_id)

                # Baseline-window sessions (no POS correlation needed if we
                # *only* compute conversion — but it's free here).
                base_events = ev_repo.fetch_for_analytics(
                    store_id, since=base_since, until=base_until
                )
                base_sessions = self._sessionise(base_events, pos_repo, store_id)

                expected_zones = self._expected_zones(db, store_id)
        except OperationalError as exc:
            logger.error("anomalies.db_unavailable", error=str(exc))
            raise DependencyUnavailableError(
                "Database is unavailable; anomalies cannot be computed."
            ) from exc
        except SQLAlchemyError:
            logger.exception("anomalies.db_error")
            raise

        anomalies: list[Anomaly] = []

        anomalies.extend(
            detect_queue_spike(cur_events, store_id=store_id, now=now)
        )
        anomalies.extend(
            detect_conversion_drop(
                cur_sessions,
                base_sessions,
                store_id=store_id,
                current_window=(cur_since, cur_until),
                baseline_window=(base_since, base_until),
                now=now,
            )
        )
        anomalies.extend(
            detect_dead_zone(
                cur_sessions,
                store_id=store_id,
                expected_zones=expected_zones,
                window=(cur_since, cur_until),
                now=now,
            )
        )

        anomalies.sort(
            key=lambda a: (_SEVERITY_RANK[a.severity], -a.detected_at.timestamp())
        )
        return anomalies

    # ── Internals ────────────────────────────────────────────────────

    def _sessionise(self, events, pos_repo, store_id: str):
        sessions = build_sessions(
            events, session_gap_minutes=self.settings.session_gap_minutes
        )
        if not events:
            return sessions
        ts_list = [e["timestamp"] for e in events]
        lo = min(ts_list) - timedelta(hours=1)
        hi = max(ts_list) + timedelta(hours=1)
        pos_rows = pos_repo.fetch_in_window(store_id, lo, hi)
        return correlate_purchases(
            sessions,
            pos_rows,
            window_minutes=self.settings.pos_correlation_window_minutes,
        )

    def _expected_zones(self, db, store_id: str) -> list[str]:
        """Pull `zone_id`s from the store_layouts table; fall back to the
        Brigade Road defaults so demos still work without a layout in the DB.
        """
        from app.infra.repositories.layout_repo import StoreLayoutRepository

        payload = StoreLayoutRepository(db).get(store_id)
        if payload and isinstance(payload, dict) and "zones" in payload:
            zones = [z.get("zone_id") for z in payload["zones"] if z.get("zone_id")]
            if zones:
                return zones
        # Default: real Brigade Road zones (Batch 5).
        from pipeline.layouts import brigade_road_layout

        return [z.zone_id for z in brigade_road_layout(store_id=store_id).zones]
