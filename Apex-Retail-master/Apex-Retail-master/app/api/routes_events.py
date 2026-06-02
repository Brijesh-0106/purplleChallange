"""POST /events/ingest — bulk event ingestion endpoint.

Contract (per challenge brief, Part B):

    Request:  JSON body {"events": [Event, ...]}, batch ≤ EVENT_INGEST_BATCH_MAX.
    Response: 202 with per-event outcomes (accepted / duplicate / rejected).
    Idempotency: same `event_id` re-sent → no new row, status="duplicate".
    Errors:   422 for schema violations (handled by FastAPI),
              413 if batch exceeds the cap,
              503 if DB is unreachable (`DependencyUnavailableError`),
              500 otherwise (sanitised envelope).

Why 202 (Accepted) instead of 201 (Created)?
    The batch may be partially-successful. 202 better reflects "we received
    the batch and processed it as far as we can" — clients use the per-event
    `results` array to reconcile.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, status

from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.events import (
    EventBatchIngestRequest,
    EventBatchIngestResponse,
    IngestEventResult,
)
from app.services.ingestion import IngestionService

router = APIRouter(prefix="/events", tags=["events"])


class BatchTooLargeError(AppError):
    """413: caller exceeded EVENT_INGEST_BATCH_MAX."""

    code = "BATCH_TOO_LARGE"
    http_status = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


@router.post(
    "/ingest",
    response_model=EventBatchIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of detection events (idempotent)",
    responses={
        413: {"description": "Batch exceeds the configured maximum"},
        422: {"description": "One or more events failed schema validation"},
        503: {"description": "Database unavailable — retry the same batch later"},
    },
)
async def ingest_events(payload: EventBatchIngestRequest) -> EventBatchIngestResponse:
    settings = get_settings()
    cap = settings.event_ingest_batch_max
    n = len(payload.events)

    # Bind early so even an early-return error log carries the count.
    structlog.contextvars.bind_contextvars(event_count=n)

    if n > cap:
        raise BatchTooLargeError(
            f"Batch contains {n} events; maximum is {cap}.",
            details={"received": n, "max": cap},
        )

    summary = IngestionService().ingest(payload.events)

    return EventBatchIngestResponse(
        accepted=summary.accepted,
        duplicates=summary.duplicates,
        rejected=summary.rejected,
        results=[
            IngestEventResult(event_id=o.event_id, status=o.status, reason=o.reason)
            for o in summary.outcomes
        ],
    )
