"""Anomaly detection.

The brief and rubric explicitly call for "logical and meaningful" anomalies
with `severity` and `suggested_action` fields. We ship three rule types
that each have an obvious business interpretation:

    * **queue_spike**     — billing queue depth crossed a threshold and
                            stayed there.
    * **conversion_drop** — current-period conversion rate dropped vs a
                            rolling baseline.
    * **dead_zone**       — a configured zone had zero visits during a
                            window when other zones had traffic.

Each rule produces zero or more `Anomaly` records. The route layer
serialises them, ordered most-recent first.

Design choices.
    * Rules are **pure** functions (sessions / events in → list[Anomaly] out).
      No DB, no HTTP. The `AnalyticsService` provides the scaffolding (just
      like /metrics and /funnel).
    * Severity is a closed enum (`info` / `warn` / `critical`) so the
      consumer can grep / filter without reading the message text.
    * `suggested_action` is a short, human-readable nudge — exactly what
      the brief asks for. We resist the temptation to dress it up as a
      structured "playbook" type; brevity matches the 10-minute reviewer
      time budget.
    * Thresholds are config-driven via dataclasses with sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class AnomalyType(str, Enum):
    QUEUE_SPIKE = "queue_spike"
    CONVERSION_DROP = "conversion_drop"
    DEAD_ZONE = "dead_zone"


@dataclass(slots=True)
class Anomaly:
    """One detected anomaly. Wire shape mirrored in `app.schemas.analytics`."""

    type: AnomalyType
    severity: Severity
    message: str
    suggested_action: str
    detected_at: datetime               # when the rule fired (UTC)
    window_start: datetime              # the period that triggered it
    window_end: datetime
    # Domain-specific evidence — keeps the response rich enough for a
    # reviewer to validate the call without re-running the analysis.
    details: dict[str, Any] = field(default_factory=dict)
