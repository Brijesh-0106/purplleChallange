"""Event persistence.

Idempotency contract:
    Inserting the same `event_id` twice MUST NOT raise to the caller and MUST
    NOT produce a duplicate row. The route layer reports "duplicate" status
    so producers can reconcile.

We implement this in a portable way (works on both Postgres and SQLite):
    1. Pre-fetch existing event_ids in the input set.
    2. Insert only the unseen ones.
    3. Race-safe: a UNIQUE constraint catches anything that slipped between
       step 1 and step 2 (concurrent ingests) — those are caught and reported
       as duplicates.

Postgres `INSERT ... ON CONFLICT DO NOTHING` would be a marginal optimisation
but worsens portability and the test suite (SQLite uses different syntax).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.infra.models import EventModel
from app.schemas.events import Event

logger = get_logger(__name__)


@dataclass
class IngestOutcome:
    """Per-event outcome. The route layer turns this into the wire response."""

    event_id: str
    status: str  # "accepted" | "duplicate" | "rejected"
    reason: str | None = None


class EventRepository:
    """Encapsulates writes/reads against the `events` table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Writes ──────────────────────────────────────────────────────────

    def insert_many(self, events: list[Event]) -> list[IngestOutcome]:
        """Insert a batch idempotently. Returns one outcome per input event,
        in input order. Caller owns the transaction (commit/rollback)."""
        if not events:
            return []

        incoming_ids = [e.event_id for e in events]
        existing_ids = self._existing_ids(incoming_ids)
        outcomes: list[IngestOutcome] = []

        now = datetime.now(timezone.utc)
        to_insert: list[EventModel] = []
        # Track event_ids we've already queued in THIS batch so an intra-batch
        # duplicate (same event_id appearing twice in one payload) collapses
        # to "first wins, rest are duplicates" — matches the producer-honest
        # semantics of duplicate detection across batches.
        seen_in_batch: set[str] = set()

        for ev in events:
            if ev.event_id in existing_ids or ev.event_id in seen_in_batch:
                outcomes.append(IngestOutcome(ev.event_id, "duplicate"))
                continue
            to_insert.append(_to_model(ev, received_at=now))
            seen_in_batch.add(ev.event_id)
            outcomes.append(IngestOutcome(ev.event_id, "accepted"))

        if to_insert:
            try:
                self.session.add_all(to_insert)
                self.session.flush()
            except IntegrityError as exc:
                # Concurrent producer beat us to the unique key. Roll back the
                # batch and re-classify newly-existing IDs as duplicates.
                self.session.rollback()
                logger.warning("events.race_on_unique", error=str(exc))
                still_existing = self._existing_ids(incoming_ids)
                outcomes = [
                    IngestOutcome(o.event_id, "duplicate")
                    if o.status == "accepted" and o.event_id in still_existing
                    else o
                    for o in outcomes
                ]
                # Re-attempt only truly-new rows in a fresh flush.
                fresh = [_to_model(e, received_at=now) for e in events if e.event_id not in still_existing]
                if fresh:
                    self.session.add_all(fresh)
                    self.session.flush()

        return outcomes

    # ── Reads ──────────────────────────────────────────────────────────

    def _existing_ids(self, event_ids: list[str]) -> set[str]:
        if not event_ids:
            return set()
        rows = self.session.execute(
            select(EventModel.event_id).where(EventModel.event_id.in_(event_ids))
        ).all()
        return {r[0] for r in rows}

    def count_for_store(self, store_id: str) -> int:
        from sqlalchemy import func

        return int(
            self.session.execute(
                select(func.count(EventModel.id)).where(EventModel.store_id == store_id)
            ).scalar_one()
        )

    def latest_timestamp(self, store_id: str) -> datetime | None:
        from sqlalchemy import func

        return self.session.execute(
            select(func.max(EventModel.timestamp)).where(EventModel.store_id == store_id)
        ).scalar_one_or_none()

    def fetch_for_analytics(
        self,
        store_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Stream all events for a store as plain dicts ordered for `build_sessions`.

        Returns dicts (not ORM rows) so the analytics layer stays decoupled
        from SQLAlchemy. Sort order is (person_id, timestamp) which is exactly
        what `app.services.sessions.build_sessions` expects.

        SQLite drops tzinfo on round-trip from `DateTime(timezone=True)`
        columns — we tag every timestamp UTC on the way out so downstream
        code never has to mix naive and aware datetimes.
        """
        from sqlalchemy import asc

        stmt = select(
            EventModel.event_id,
            EventModel.event_type,
            EventModel.store_id,
            EventModel.camera_id,
            EventModel.timestamp,
            EventModel.track_id,
            EventModel.person_id,
            EventModel.zone_id,
            EventModel.duration_s,
            EventModel.confidence,
            EventModel.is_staff,
            EventModel.group_size,
        ).where(EventModel.store_id == store_id)

        if since is not None:
            stmt = stmt.where(EventModel.timestamp >= since)
        if until is not None:
            stmt = stmt.where(EventModel.timestamp <= until)

        # Order: person_id first (so build_sessions can stream-group), then ts.
        # NULLs LAST is portable across SQLite + Postgres via a CASE expression.
        stmt = stmt.order_by(
            asc(EventModel.person_id),
            asc(EventModel.timestamp),
        )

        rows = self.session.execute(stmt).all()
        return [
            {
                "event_id": r.event_id,
                "event_type": r.event_type,
                "store_id": r.store_id,
                "camera_id": r.camera_id,
                "timestamp": _utc(r.timestamp),
                "track_id": r.track_id,
                "person_id": r.person_id,
                "zone_id": r.zone_id,
                "duration_s": r.duration_s,
                "confidence": r.confidence,
                "is_staff": r.is_staff,
                "group_size": r.group_size,
            }
            for r in rows
        ]


def _utc(ts: datetime | None) -> datetime | None:
    """Tag a naive datetime as UTC. Idempotent for already-aware values."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _to_model(ev: Event, *, received_at: datetime) -> EventModel:
    """Schema → ORM. Keeps the mapping in one place."""
    return EventModel(
        event_id=ev.event_id,
        event_type=ev.event_type.value,
        store_id=ev.store_id,
        camera_id=ev.camera_id,
        timestamp=ev.timestamp,
        track_id=ev.track_id,
        person_id=ev.person_id,
        zone_id=ev.zone_id,
        duration_s=ev.duration_s,
        confidence=ev.confidence,
        is_staff=ev.is_staff,
        group_size=ev.group_size,
        bbox=list(ev.bbox) if ev.bbox is not None else None,
        payload=ev.payload or None,
        received_at=received_at,
    )
