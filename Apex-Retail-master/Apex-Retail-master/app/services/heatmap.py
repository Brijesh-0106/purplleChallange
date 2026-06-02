"""Heatmap service — per-zone activity report.

Produces, per zone:
    visits       — number of distinct customer sessions that entered the zone
    visit_share  — visits ÷ unique_visitors (0..1; what fraction of customers passed through)
    avg_dwell_s  — mean dwell duration for sessions that visited
    total_dwell_s — sum across customers
    intensity    — 0..1 normalised by the busiest zone (great for shading)

`data_confidence` mirrors metrics: when fewer than `min_sessions_for_confidence`
customer sessions exist, the report is flagged `low` so consumers (dashboard,
anomaly detection) can decide what to do with thin signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.services.metrics import CONFIDENCE_LOW_THRESHOLD
from app.services.sessions import Session


@dataclass(slots=True)
class ZoneHeat:
    zone_id: str
    visits: int
    visit_share: float        # 0..1
    avg_dwell_s: float
    total_dwell_s: float
    intensity: float          # 0..1 (relative to busiest zone in this report)


@dataclass(slots=True)
class HeatmapReport:
    store_id: str
    unique_visitors: int
    zones: list[ZoneHeat] = field(default_factory=list)
    data_confidence: Literal["ok", "low"] = "ok"


def compute_heatmap(
    sessions: list[Session],
    *,
    store_id: str,
    min_sessions_for_confidence: int = CONFIDENCE_LOW_THRESHOLD,
) -> HeatmapReport:
    customers = [s for s in sessions if not s.is_staff]
    unique_visitors = len({s.person_id for s in customers})

    # Aggregate per-zone visit counts and dwell totals.
    visits_by_zone: dict[str, int] = {}
    dwell_lists: dict[str, list[float]] = {}
    for s in customers:
        for zid in s.zones_visited:
            visits_by_zone[zid] = visits_by_zone.get(zid, 0) + 1
        for zid, secs in s.dwell_seconds_by_zone.items():
            dwell_lists.setdefault(zid, []).append(secs)

    # Combine: every zone that appears in EITHER counts as a heatmap row.
    all_zones = set(visits_by_zone) | set(dwell_lists)

    rows: list[ZoneHeat] = []
    for zid in sorted(all_zones):
        visits = visits_by_zone.get(zid, 0)
        dwells = dwell_lists.get(zid, [])
        total = round(sum(dwells), 2)
        avg = round(total / len(dwells), 2) if dwells else 0.0
        share = round(visits / unique_visitors, 4) if unique_visitors > 0 else 0.0
        rows.append(
            ZoneHeat(
                zone_id=zid,
                visits=visits,
                visit_share=share,
                avg_dwell_s=avg,
                total_dwell_s=total,
                intensity=0.0,  # filled below after we know the max
            )
        )

    # Normalise intensity by visit count of the busiest zone.
    if rows:
        max_visits = max(r.visits for r in rows) or 1
        for r in rows:
            r.intensity = round(r.visits / max_visits, 4)

    confidence: Literal["ok", "low"] = (
        "low" if len(customers) < min_sessions_for_confidence else "ok"
    )

    return HeatmapReport(
        store_id=store_id,
        unique_visitors=unique_visitors,
        zones=rows,
        data_confidence=confidence,
    )
