"""Pydantic response schemas for analytics endpoints.

The service layer returns plain `@dataclass`es (sessions, metrics, funnel,
heatmap). These DTOs are the wire shape — keeping them separate keeps
internal changes from breaking the API contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MetricsResponse(BaseModel):
    """Wire shape for `GET /stores/{store_id}/metrics`."""

    model_config = ConfigDict(json_schema_extra={"example": {
        "store_id": "ST1008",
        "window_start": "2026-06-01T12:00:00+00:00",
        "window_end": "2026-06-01T12:01:30+00:00",
        "unique_visitors": 6,
        "staff_count": 1,
        "sessions_total": 8,
        "total_purchases": 1,
        "gross_basket_inr": 274.36,
        "conversion_rate": 0.1667,
        "avg_dwell_seconds": 22.5,
        "billing_queue_joins": 2,
        "billing_queue_abandons": 1,
        "billing_abandonment_rate": 0.5,
        "data_confidence": "low",
        "avg_dwell_by_zone_seconds": {"Z_FOH": 12.0, "Z_MAKEUP": 18.5},
    }})

    store_id: str
    window_start: datetime | None
    window_end: datetime | None

    unique_visitors: int = Field(ge=0)
    staff_count: int = Field(ge=0)
    sessions_total: int = Field(ge=0)

    total_purchases: int = Field(ge=0)
    gross_basket_inr: float = Field(ge=0)

    conversion_rate: float = Field(ge=0, le=1)
    avg_dwell_seconds: float = Field(ge=0)

    billing_queue_joins: int = Field(ge=0)
    billing_queue_abandons: int = Field(ge=0)
    billing_abandonment_rate: float = Field(ge=0, le=1)

    data_confidence: Literal["ok", "low"]
    avg_dwell_by_zone_seconds: dict[str, float]


class FunnelStageResponse(BaseModel):
    name: Literal["entry", "browse", "billing", "purchase"]
    sessions: int = Field(ge=0)
    drop_off_from_previous: float = Field(ge=0, le=1)


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStageResponse]
    overall_conversion: float = Field(ge=0, le=1)


class ZoneHeatResponse(BaseModel):
    zone_id: str
    visits: int = Field(ge=0)
    visit_share: float = Field(ge=0, le=1)
    avg_dwell_s: float = Field(ge=0)
    total_dwell_s: float = Field(ge=0)
    intensity: float = Field(ge=0, le=1)


class HeatmapResponse(BaseModel):
    store_id: str
    unique_visitors: int = Field(ge=0)
    zones: list[ZoneHeatResponse]
    data_confidence: Literal["ok", "low"]


# ──────────────────────────────────────────────────────────────────
# Anomalies
# ──────────────────────────────────────────────────────────────────


class AnomalyResponse(BaseModel):
    """Wire shape for one entry in `GET /stores/{store_id}/anomalies`."""

    type: Literal["queue_spike", "conversion_drop", "dead_zone"]
    severity: Literal["info", "warn", "critical"]
    message: str
    suggested_action: str
    detected_at: datetime
    window_start: datetime
    window_end: datetime
    details: dict


class AnomaliesListResponse(BaseModel):
    store_id: str
    window_start: datetime
    window_end: datetime
    count: int = Field(ge=0)
    anomalies: list[AnomalyResponse]
