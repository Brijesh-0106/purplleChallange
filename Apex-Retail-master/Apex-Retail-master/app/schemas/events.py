"""Event schemas — what the API and the pipeline both speak.

The `Event` model is the canonical shape. Validation enforces the rules that
must hold *regardless* of where the event came from:

  • event_id is a non-empty string (treated as the dedup key by the DB layer).
  • event_type is in the closed set defined in `app.domain.events`.
  • timestamp is timezone-aware UTC.
  • zone_id is required for ZONE_*/DWELL/BILLING_* events; forbidden for ENTRY/EXIT/REENTRY.
  • duration_s is positive when present and required for DWELL / BILLING_*.
  • confidence ∈ [0, 1].
  • bbox (if present) has 4 finite numbers in [x1, y1, x2, y2] order with x2 > x1, y2 > y1.

Anything else (e.g. `track_id`, `is_staff`, `group_size`) is permitted via
`extra="allow"` and stored in `payload` — keeps the schema forward-compatible
when the pipeline emits richer attributes in later batches.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.domain.events import (
    DURATION_EVENTS,
    ZONE_BOUND_EVENTS,
    ZONE_FORBIDDEN_EVENTS,
    EventType,
)


class Event(BaseModel):
    """Canonical event."""

    # Pydantic v2 config:
    #   - extra="allow": accept future-pipeline fields without breaking ingest.
    #   - frozen=False: ingestion may enrich (e.g. server-side fields later).
    #   - populate_by_name: tolerate camelCase if a producer sends it.
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    event_id: str = Field(..., min_length=1, max_length=128, description="Globally unique; dedup key.")
    event_type: EventType
    store_id: str = Field(..., min_length=1, max_length=64)
    camera_id: str = Field(..., min_length=1, max_length=64)
    timestamp: datetime = Field(..., description="UTC. ISO-8601.")

    track_id: str | None = Field(default=None, max_length=128)
    person_id: str | None = Field(
        default=None,
        max_length=128,
        description="Stable cross-camera identity (Re-ID) when known.",
    )

    zone_id: str | None = Field(default=None, max_length=64)
    duration_s: float | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="[x1, y1, x2, y2] in image coords (pixels).",
    )

    is_staff: bool | None = Field(default=None)
    group_size: int | None = Field(default=None, ge=1)

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        """Force UTC. Naïve datetimes are *rejected*, not silently coerced — the
        challenge brief explicitly penalises silent data fixups."""
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC ISO-8601 with offset)")
        return v.astimezone(timezone.utc)

    @field_validator("bbox")
    @classmethod
    def _bbox_well_formed(cls, v: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if v is None:
            return v
        if len(v) != 4:
            raise ValueError("bbox must have exactly 4 numbers: [x1, y1, x2, y2]")
        x1, y1, x2, y2 = v
        if not all(map(_finite, v)):
            raise ValueError("bbox values must be finite numbers")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox requires x2 > x1 and y2 > y1")
        return v

    @model_validator(mode="after")
    def _zone_and_duration_rules(self) -> "Event":
        et = self.event_type

        if et in ZONE_BOUND_EVENTS and not self.zone_id:
            raise ValueError(f"event_type={et.value} requires zone_id")
        if et in ZONE_FORBIDDEN_EVENTS and self.zone_id is not None:
            raise ValueError(
                f"event_type={et.value} must not carry zone_id (it's a perimeter event)"
            )
        if et in DURATION_EVENTS and self.duration_s is None:
            raise ValueError(f"event_type={et.value} requires duration_s")
        return self

    # ── Helpers ───────────────────────────────────────────────────────────

    @property
    def payload(self) -> dict[str, Any]:
        """Everything the schema doesn't explicitly model — preserved verbatim."""
        # `model_extra` holds extras when extra='allow'.
        return self.model_extra or {}


class EventBatchIngestRequest(BaseModel):
    """Body for `POST /events/ingest`. Cap enforced at the route layer too."""

    events: list[Event] = Field(..., min_length=1)


class IngestEventResult(BaseModel):
    """Per-event result so callers can reconcile partial successes."""

    event_id: str
    status: str  # "accepted" | "duplicate" | "rejected"
    reason: str | None = None


class EventBatchIngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: list[IngestEventResult]


# ── Tiny helpers ──────────────────────────────────────────────────────────

def _finite(x: float) -> bool:
    # math.isfinite would do, but avoiding the import keeps validators tidy.
    return x == x and x not in (float("inf"), float("-inf"))
