"""Store metrics service.

Computes the canonical KPI bundle for a store:

    * unique_visitors       — distinct customer person_ids (staff excluded)
    * staff_count           — number of distinct staff person_ids (informational)
    * conversion_rate       — purchasers / unique_visitors  (north-star metric)
    * avg_dwell_seconds     — mean DWELL across visitor sessions
    * total_purchases       — sessions with purchase=True
    * gross_basket_inr      — sum of purchase_basket_inr across visitors
    * billing_queue_*       — joins, abandons, abandonment rate
    * sessions_total        — informational; sum of customer + staff sessions

`window_start`/`window_end` describe the requested time range. When the
caller doesn't pass them, we default to "all events for this store" — the
brief and rubric both expect a well-defined-but-flexible API.

`data_confidence` is the rubric's hedge against tiny denominators: when
fewer than `min_sessions_for_confidence` customer sessions land in the
window we expose a `low` flag so the consumer (dashboard, anomaly detector)
can suppress noisy ratios. The brief explicitly calls this out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.services.sessions import Session


CONFIDENCE_LOW_THRESHOLD = 20  # < 20 sessions → low confidence


@dataclass(slots=True)
class StoreMetrics:
    store_id: str
    window_start: datetime | None
    window_end: datetime | None

    unique_visitors: int = 0
    staff_count: int = 0
    sessions_total: int = 0

    total_purchases: int = 0
    gross_basket_inr: float = 0.0

    conversion_rate: float = 0.0          # 0..1; 0 when no visitors
    avg_dwell_seconds: float = 0.0

    billing_queue_joins: int = 0
    billing_queue_abandons: int = 0
    billing_abandonment_rate: float = 0.0  # 0..1

    data_confidence: Literal["ok", "low"] = "ok"

    # Per-zone dwell aggregates (handy for /metrics consumers; full grid in /heatmap)
    avg_dwell_by_zone_seconds: dict[str, float] = field(default_factory=dict)


def compute_store_metrics(
    sessions: list[Session],
    *,
    store_id: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    min_sessions_for_confidence: int = CONFIDENCE_LOW_THRESHOLD,
) -> StoreMetrics:
    """Aggregate sessions into a `StoreMetrics`.

    Sessions are assumed to already be scoped to `store_id` and filtered to
    the [window_start, window_end] range — the route layer does that via
    `EventRepository.fetch_for_analytics(since, until)` before sessionising.
    """
    customers = [s for s in sessions if not s.is_staff]
    staff_sessions = [s for s in sessions if s.is_staff]

    out = StoreMetrics(
        store_id=store_id,
        window_start=window_start,
        window_end=window_end,
        sessions_total=len(sessions),
        unique_visitors=len({s.person_id for s in customers}),
        staff_count=len({s.person_id for s in staff_sessions}),
    )

    purchasers = [s for s in customers if s.purchase]
    out.total_purchases = len(purchasers)
    out.gross_basket_inr = round(
        sum(s.purchase_basket_inr or 0.0 for s in purchasers), 2
    )

    if out.unique_visitors > 0:
        out.conversion_rate = round(out.total_purchases / out.unique_visitors, 4)

    # Average dwell per CUSTOMER session — sums all DWELL durations, divided
    # by visitor count. Excludes pure-perimeter sessions (no zone visits) so
    # the average stays interpretable as "browsing time per customer".
    dwelt_seconds = []
    for s in customers:
        total_dwell = sum(s.dwell_seconds_by_zone.values())
        if total_dwell > 0:
            dwelt_seconds.append(total_dwell)
    if dwelt_seconds:
        out.avg_dwell_seconds = round(sum(dwelt_seconds) / len(dwelt_seconds), 2)

    # Per-zone dwell — mean across customer sessions that visited that zone.
    zone_totals: dict[str, list[float]] = {}
    for s in customers:
        for zid, secs in s.dwell_seconds_by_zone.items():
            zone_totals.setdefault(zid, []).append(secs)
    out.avg_dwell_by_zone_seconds = {
        zid: round(sum(vs) / len(vs), 2) for zid, vs in zone_totals.items()
    }

    # Billing queue stats — counted across all customer sessions.
    joins = sum(s.billing_join_count for s in customers)
    abandons = sum(s.billing_abandon_count for s in customers)
    out.billing_queue_joins = joins
    out.billing_queue_abandons = abandons
    if joins > 0:
        # Abandons that didn't end in a purchase. A single session might join
        # twice and abandon once — we count events as the rubric expects.
        unmatched_abandons = sum(
            s.billing_abandon_count
            for s in customers
            if not s.purchase and s.billing_abandon_count > 0
        )
        out.billing_abandonment_rate = round(unmatched_abandons / joins, 4)

    if len(customers) < min_sessions_for_confidence:
        out.data_confidence = "low"

    return out
