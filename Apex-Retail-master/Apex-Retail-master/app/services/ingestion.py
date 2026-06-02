"""IngestionService.

Orchestrates a single ingest call:

    1. Open a transactional session (or fail fast with `DependencyUnavailableError`
       if the DB is unreachable — surfaced as 503 by the route).
    2. Hand the validated events to `EventRepository.insert_many` for idempotent
       persistence.
    3. Aggregate the per-event outcomes into a stable summary the route returns.
    4. Best-effort broadcast a fresh metrics snapshot per affected store so the
       live dashboard refreshes without polling.

Validation has already happened at the schema layer — this service trusts the
inputs are syntactically valid and focuses on persistence + orchestration.

Broadcast is best-effort: a broadcaster failure NEVER fails ingest. The
events are already durable in the DB by the time we publish.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.errors import DependencyUnavailableError
from app.core.logging import get_logger
from app.infra.db import session_scope
from app.infra.repositories.events_repo import EventRepository, IngestOutcome
from app.schemas.events import Event
from app.services.broadcaster import get_broadcaster

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    """Plain DTO returned to the route layer."""

    accepted: int
    duplicates: int
    rejected: int
    outcomes: list[IngestOutcome]


class IngestionService:
    """Single entrypoint for event ingestion."""

    def ingest(self, events: list[Event]) -> IngestionSummary:
        """Persist a batch idempotently and return a per-event summary.

        Raises:
            DependencyUnavailableError: DB is unreachable (route returns 503).
        """
        if not events:
            return IngestionSummary(accepted=0, duplicates=0, rejected=0, outcomes=[])

        try:
            with session_scope() as session:
                repo = EventRepository(session)
                outcomes = repo.insert_many(events)
        except OperationalError as exc:
            logger.error("ingest.db_unavailable", error=str(exc))
            raise DependencyUnavailableError(
                "Database is unavailable; events were not persisted.",
                details={"hint": "retry the same batch — ingest is idempotent"},
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("ingest.db_error")
            raise

        accepted = sum(1 for o in outcomes if o.status == "accepted")
        duplicates = sum(1 for o in outcomes if o.status == "duplicate")
        rejected = sum(1 for o in outcomes if o.status == "rejected")

        structlog.contextvars.bind_contextvars(
            event_count=len(events),
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
        )
        logger.info(
            "ingest.completed",
            event_count=len(events),
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
        )

        # Best-effort: broadcast a "stores changed" ping per affected store.
        # The dashboard turns each ping into a /metrics fetch — keeps the
        # WS payload tiny and the data fresh.
        if accepted > 0:
            self._broadcast_changes(events)

        return IngestionSummary(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            outcomes=outcomes,
        )

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _broadcast_changes(events: list[Event]) -> None:
        """Notify dashboard subscribers per affected store. Never raises."""
        try:
            stores = {e.store_id for e in events}
            br = get_broadcaster()
            loop = asyncio.get_event_loop()
            for sid in stores:
                payload = {
                    "type": "store_changed",
                    "store_id": sid,
                    "event_count": sum(1 for e in events if e.store_id == sid),
                }
                # Schedule the publish on the running loop (FastAPI is async).
                if loop.is_running():
                    asyncio.create_task(br.publish(sid, payload))
                else:
                    # Sync context (tests, scripts) — run to completion.
                    loop.run_until_complete(br.publish(sid, payload))
        except Exception as exc:  # noqa: BLE001 — broadcast must never break ingest
            logger.debug("ingest.broadcast_failed", error=str(exc))
