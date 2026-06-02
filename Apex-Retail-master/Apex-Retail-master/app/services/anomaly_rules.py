"""Anomaly detection rules.

Each rule is a pure function: takes whatever it needs (events, sessions,
config) and returns a list of `Anomaly` records.

Rules.
    detect_queue_spike      — derives time-bucketed queue depth from
                              BILLING_QUEUE_JOIN/ABANDON events; flags
                              sustained spikes.
    detect_conversion_drop  — compares current-window conversion rate to a
                              prior-window baseline; flags a meaningful drop.
    detect_dead_zone        — zones that had zero visits while other zones
                              had traffic during the window.

Why these three?
    They map directly to the brief's three illustrative anomaly examples
    ("queue depth spiking", "conversion below 7-day average", "dead zone")
    and each surfaces a distinct business concern.

Why time-bucketed (not raw event-driven)?
    Bucketing smooths out one-frame jitter and makes thresholds
    interpretable in plain English ("queue depth ≥ 7 for ≥ 60 s").
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.domain.anomaly import Anomaly, AnomalyType, Severity
from app.domain.events import EventType
from app.services.metrics import compute_store_metrics
from app.services.sessions import Session


# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QueueSpikeConfig:
    """When billing-queue depth ≥ `threshold_depth` for ≥ `min_duration_s`."""

    threshold_depth: int = 7
    min_duration_s: float = 60.0
    bucket_size_s: float = 30.0
    severity_critical_at: int = 12   # ≥ this depth → critical


@dataclass(frozen=True, slots=True)
class ConversionDropConfig:
    """Current-window conversion rate compared to a rolling prior-window baseline."""

    drop_ratio: float = 0.5            # current / baseline ≤ this → flag
    min_baseline_visitors: int = 20    # baseline ignored under this threshold
    min_current_visitors: int = 5      # don't fire on tiny denominators


@dataclass(frozen=True, slots=True)
class DeadZoneConfig:
    """A zone with zero visits while other zones had traffic."""

    min_other_visits: int = 5  # at least this many visits in OTHER zones to fire


# ──────────────────────────────────────────────────────────────────
# Rule 1: queue spike
# ──────────────────────────────────────────────────────────────────


def detect_queue_spike(
    events: Iterable[dict],
    *,
    store_id: str,
    config: QueueSpikeConfig | None = None,
    now: datetime | None = None,
) -> list[Anomaly]:
    """Bucket queue depth in `bucket_size_s` slices; flag spikes that span
    enough consecutive buckets to satisfy `min_duration_s`.

    Queue depth is the running count of people in the billing zone:
        +1 on BILLING_QUEUE_JOIN, -1 on BILLING_QUEUE_ABANDON.
    POS-correlated purchases also leave the zone, but the pipeline already
    emits BILLING_QUEUE_ABANDON for every billing-zone exit; the analytics
    layer reclassifies the abandon as a purchase via POS correlation. For
    *queue-depth* purposes both are queue exits, so the abandon event is
    the right signal regardless.
    """
    cfg = config or QueueSpikeConfig()
    now = now or datetime.now(tz=timezone.utc)

    # Build the (delta, ts) stream sorted in time.
    deltas: list[tuple[datetime, int]] = []
    for ev in events:
        et = ev.get("event_type")
        if et == EventType.BILLING_QUEUE_JOIN.value:
            deltas.append((ev["timestamp"], +1))
        elif et == EventType.BILLING_QUEUE_ABANDON.value:
            deltas.append((ev["timestamp"], -1))
    if not deltas:
        return []
    deltas.sort(key=lambda x: x[0])

    # Bucket the time axis. We use the floor of (ts - origin) / bucket_size.
    origin = deltas[0][0]
    bucket_size = timedelta(seconds=cfg.bucket_size_s)
    bucket_count = int((deltas[-1][0] - origin) / bucket_size) + 1

    # depth_at[i] = running depth at the END of bucket i, after applying
    # all deltas whose timestamp landed inside the bucket.
    depth_at = [0] * bucket_count
    running = 0
    j = 0
    for i in range(bucket_count):
        bucket_end = origin + bucket_size * (i + 1)
        while j < len(deltas) and deltas[j][0] < bucket_end:
            running += deltas[j][1]
            j += 1
        depth_at[i] = max(running, 0)

    # Find contiguous runs where depth ≥ threshold for ≥ min_duration_s.
    threshold = cfg.threshold_depth
    min_buckets = max(1, int(cfg.min_duration_s / cfg.bucket_size_s))
    out: list[Anomaly] = []

    i = 0
    while i < bucket_count:
        if depth_at[i] < threshold:
            i += 1
            continue
        # Extend the run.
        run_start = i
        peak = depth_at[i]
        while i < bucket_count and depth_at[i] >= threshold:
            peak = max(peak, depth_at[i])
            i += 1
        run_end = i  # exclusive
        if (run_end - run_start) >= min_buckets:
            window_start = origin + bucket_size * run_start
            window_end = origin + bucket_size * run_end
            severity = (
                Severity.CRITICAL if peak >= cfg.severity_critical_at else Severity.WARN
            )
            out.append(
                Anomaly(
                    type=AnomalyType.QUEUE_SPIKE,
                    severity=severity,
                    message=(
                        f"Billing queue depth peaked at {peak} for "
                        f"~{int((run_end - run_start) * cfg.bucket_size_s)} s "
                        f"(threshold {threshold})."
                    ),
                    suggested_action="Open additional billing counter or move staff to billing.",
                    detected_at=now,
                    window_start=window_start,
                    window_end=window_end,
                    details={
                        "store_id": store_id,
                        "peak_depth": peak,
                        "threshold": threshold,
                        "duration_s": (run_end - run_start) * cfg.bucket_size_s,
                    },
                )
            )

    return out


# ──────────────────────────────────────────────────────────────────
# Rule 2: conversion drop
# ──────────────────────────────────────────────────────────────────


def detect_conversion_drop(
    current: list[Session],
    baseline: list[Session],
    *,
    store_id: str,
    current_window: tuple[datetime, datetime],
    baseline_window: tuple[datetime, datetime],
    config: ConversionDropConfig | None = None,
    now: datetime | None = None,
) -> list[Anomaly]:
    """Compare current-window conversion rate to a baseline-window rate.

    Both inputs are pre-built session lists (already filtered to their
    respective windows). Staff are excluded automatically by the metrics
    function. The rule fires only when both windows have enough volume to
    be statistically meaningful — otherwise it would scream every quiet
    morning.
    """
    cfg = config or ConversionDropConfig()
    now = now or datetime.now(tz=timezone.utc)

    cur = compute_store_metrics(
        current, store_id=store_id, window_start=current_window[0], window_end=current_window[1]
    )
    base = compute_store_metrics(
        baseline, store_id=store_id, window_start=baseline_window[0], window_end=baseline_window[1]
    )

    if base.unique_visitors < cfg.min_baseline_visitors:
        return []
    if cur.unique_visitors < cfg.min_current_visitors:
        return []
    if base.conversion_rate <= 0:
        return []

    ratio = cur.conversion_rate / base.conversion_rate if base.conversion_rate else 0
    if ratio > cfg.drop_ratio:
        return []

    severity = Severity.CRITICAL if ratio < cfg.drop_ratio / 2 else Severity.WARN
    return [
        Anomaly(
            type=AnomalyType.CONVERSION_DROP,
            severity=severity,
            message=(
                f"Conversion rate {cur.conversion_rate:.2%} is "
                f"{(1 - ratio):.0%} below the baseline of "
                f"{base.conversion_rate:.2%} ({base.unique_visitors} visitors)."
            ),
            suggested_action=(
                "Investigate billing-queue length, staff availability, "
                "or stockouts on top SKUs."
            ),
            detected_at=now,
            window_start=current_window[0],
            window_end=current_window[1],
            details={
                "store_id": store_id,
                "current_conversion": cur.conversion_rate,
                "baseline_conversion": base.conversion_rate,
                "current_visitors": cur.unique_visitors,
                "baseline_visitors": base.unique_visitors,
                "ratio_to_baseline": round(ratio, 4),
            },
        )
    ]


# ──────────────────────────────────────────────────────────────────
# Rule 3: dead zone
# ──────────────────────────────────────────────────────────────────


def detect_dead_zone(
    sessions: list[Session],
    *,
    store_id: str,
    expected_zones: list[str],
    window: tuple[datetime, datetime],
    config: DeadZoneConfig | None = None,
    now: datetime | None = None,
) -> list[Anomaly]:
    """Flag any zone in `expected_zones` that received zero visits while
    other zones had at least `min_other_visits` total.
    """
    cfg = config or DeadZoneConfig()
    now = now or datetime.now(tz=timezone.utc)

    visits_per_zone: dict[str, int] = defaultdict(int)
    for s in sessions:
        if s.is_staff:
            continue
        for zid in s.zones_visited:
            visits_per_zone[zid] += 1

    other_visits = sum(visits_per_zone.values())
    out: list[Anomaly] = []
    for zid in expected_zones:
        if visits_per_zone.get(zid, 0) == 0 and other_visits >= cfg.min_other_visits:
            out.append(
                Anomaly(
                    type=AnomalyType.DEAD_ZONE,
                    severity=Severity.WARN,
                    message=f"Zone {zid} had zero customer visits in the window.",
                    suggested_action=(
                        f"Check signage, lighting, or display layout in {zid}."
                    ),
                    detected_at=now,
                    window_start=window[0],
                    window_end=window[1],
                    details={
                        "store_id": store_id,
                        "zone_id": zid,
                        "other_zones_total_visits": other_visits,
                    },
                )
            )
    return out
