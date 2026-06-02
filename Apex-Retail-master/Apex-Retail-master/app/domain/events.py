"""Event taxonomy.

Source of truth for **every** event type the system understands. The Pydantic
schema, the DB model, the pipeline emitter, and the test assertions all import
from this module — there must be exactly one place where event names live.

Reference: Purplle Tech Challenge 2026 Round 2 Problem Statement, Part A.
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """Closed set of event types emitted by the detection pipeline.

    Inheriting from `str` so JSON serialisation is `"ENTRY"`, not `"EventType.ENTRY"`.
    """

    # --- Entry / exit ---
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    REENTRY = "REENTRY"

    # --- Zone activity ---
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    DWELL = "DWELL"

    # --- Billing / queue ---
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"

    @classmethod
    def values(cls) -> list[str]:
        return [e.value for e in cls]


# Event types that REQUIRE a zone_id to make sense.
ZONE_BOUND_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.ZONE_ENTER,
        EventType.ZONE_EXIT,
        EventType.DWELL,
        EventType.BILLING_QUEUE_JOIN,
        EventType.BILLING_QUEUE_ABANDON,
    }
)

# Event types that should NOT carry a zone_id (entry/exit are at the store
# perimeter, not a zone).
ZONE_FORBIDDEN_EVENTS: frozenset[EventType] = frozenset(
    {EventType.ENTRY, EventType.EXIT, EventType.REENTRY}
)

# Events that record a duration (in seconds).
DURATION_EVENTS: frozenset[EventType] = frozenset(
    {EventType.DWELL, EventType.BILLING_QUEUE_JOIN, EventType.BILLING_QUEUE_ABANDON}
)
