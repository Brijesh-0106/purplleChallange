# PROMPT (Claude, Batch 7):
#   "Test the three anomaly rules independently of the DB:
#    (1) detect_queue_spike fires when bucketed depth ≥ threshold for ≥
#        min_duration_s; severity escalates to CRITICAL beyond
#        severity_critical_at; doesn't fire on short blips.
#    (2) detect_conversion_drop fires when current/baseline conversion
#        falls below the configured ratio; doesn't fire on tiny
#        denominators; doesn't fire when baseline conversion is zero.
#    (3) detect_dead_zone fires for zones with zero visits when other
#        zones have at least min_other_visits; suppressed on a quiet day."

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.anomaly import AnomalyType, Severity
from app.services.anomaly_rules import (
    ConversionDropConfig,
    DeadZoneConfig,
    QueueSpikeConfig,
    detect_conversion_drop,
    detect_dead_zone,
    detect_queue_spike,
)
from app.services.sessions import Session

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────
# Queue spike
# ──────────────────────────────────────────────────────────────────


def _queue_event(t_offset_s: float, et: str) -> dict:
    return {
        "event_type": et,
        "timestamp": T0 + timedelta(seconds=t_offset_s),
    }


def test_queue_spike_fires_when_threshold_held() -> None:
    # 8 joins in the first 5 s → depth = 8.
    # 1 abandon at t=120s → depth = 7. Threshold is 7, held for ~120s.
    events = [_queue_event(i * 0.5, "BILLING_QUEUE_JOIN") for i in range(8)]
    events.append(_queue_event(120.0, "BILLING_QUEUE_ABANDON"))

    out = detect_queue_spike(
        events,
        store_id="ST1008",
        config=QueueSpikeConfig(
            threshold_depth=7, min_duration_s=60.0, bucket_size_s=30.0,
            severity_critical_at=12,
        ),
    )
    assert len(out) == 1
    assert out[0].type == AnomalyType.QUEUE_SPIKE
    assert out[0].severity == Severity.WARN
    assert out[0].details["peak_depth"] == 8


def test_queue_spike_critical_at_high_depth() -> None:
    events = [_queue_event(i * 0.5, "BILLING_QUEUE_JOIN") for i in range(15)]
    events.append(_queue_event(120.0, "BILLING_QUEUE_ABANDON"))

    out = detect_queue_spike(
        events,
        store_id="ST1008",
        config=QueueSpikeConfig(
            threshold_depth=7, min_duration_s=60.0, bucket_size_s=30.0,
            severity_critical_at=12,
        ),
    )
    assert out[0].severity == Severity.CRITICAL


def test_queue_spike_short_blip_suppressed() -> None:
    # 8 join → 8 abandon within 5 s = ≤ 1 bucket above threshold.
    events = [_queue_event(i * 0.1, "BILLING_QUEUE_JOIN") for i in range(8)]
    events.extend(
        _queue_event(2.0 + i * 0.1, "BILLING_QUEUE_ABANDON") for i in range(8)
    )
    out = detect_queue_spike(
        events,
        store_id="ST1008",
        config=QueueSpikeConfig(
            threshold_depth=7, min_duration_s=60.0, bucket_size_s=30.0,
            severity_critical_at=12,
        ),
    )
    assert out == []


def test_queue_spike_no_billing_events_no_anomaly() -> None:
    out = detect_queue_spike([], store_id="ST1008")
    assert out == []


# ──────────────────────────────────────────────────────────────────
# Conversion drop
# ──────────────────────────────────────────────────────────────────


def _session(pid, *, store="ST1008", purchase=False, billed=True, staff=False) -> Session:
    s = Session(
        store_id=store,
        person_id=pid,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=60),
        is_staff=staff,
    )
    if billed:
        s.billing_join_count = 1
    if purchase:
        s.purchase = True
        s.purchase_basket_inr = 999.0
    return s


def test_conversion_drop_fires_when_below_ratio() -> None:
    # Baseline: 25 visitors, 10 purchases → 0.40
    # Current : 10 visitors, 1 purchase  → 0.10  (ratio 0.25 < 0.5)
    baseline = (
        [_session(f"B-buy-{i}", purchase=True) for i in range(10)]
        + [_session(f"B-vis-{i}") for i in range(15)]
    )
    current = (
        [_session(f"C-buy-1", purchase=True)]
        + [_session(f"C-vis-{i}") for i in range(9)]
    )

    cur_win = (T0, T0 + timedelta(hours=1))
    base_win = (T0 - timedelta(hours=24), T0)

    out = detect_conversion_drop(
        current,
        baseline,
        store_id="ST1008",
        current_window=cur_win,
        baseline_window=base_win,
    )
    assert len(out) == 1
    a = out[0]
    assert a.type == AnomalyType.CONVERSION_DROP
    assert a.severity in (Severity.WARN, Severity.CRITICAL)
    assert a.details["current_conversion"] == 0.1
    assert a.details["baseline_conversion"] == 0.4


def test_conversion_drop_suppressed_when_baseline_too_small() -> None:
    baseline = [_session(f"B-{i}", purchase=(i < 1)) for i in range(5)]   # 5 < 20
    current = [_session(f"C-{i}", purchase=(i < 1)) for i in range(10)]
    cur_win = (T0, T0 + timedelta(hours=1))
    base_win = (T0 - timedelta(hours=24), T0)

    out = detect_conversion_drop(
        current, baseline, store_id="ST1008",
        current_window=cur_win, baseline_window=base_win,
    )
    assert out == []


def test_conversion_drop_suppressed_when_current_too_small() -> None:
    baseline = [_session(f"B-{i}", purchase=(i < 10)) for i in range(25)]
    current = [_session(f"C-{i}", purchase=False) for i in range(3)]   # < 5
    cur_win = (T0, T0 + timedelta(hours=1))
    base_win = (T0 - timedelta(hours=24), T0)

    out = detect_conversion_drop(
        current, baseline, store_id="ST1008",
        current_window=cur_win, baseline_window=base_win,
    )
    assert out == []


def test_conversion_drop_suppressed_when_baseline_zero() -> None:
    baseline = [_session(f"B-{i}", purchase=False) for i in range(25)]
    current = [_session(f"C-{i}", purchase=False) for i in range(10)]
    cur_win = (T0, T0 + timedelta(hours=1))
    base_win = (T0 - timedelta(hours=24), T0)

    out = detect_conversion_drop(
        current, baseline, store_id="ST1008",
        current_window=cur_win, baseline_window=base_win,
    )
    assert out == []


# ──────────────────────────────────────────────────────────────────
# Dead zone
# ──────────────────────────────────────────────────────────────────


def _session_in(zone_ids: list[str]) -> Session:
    s = _session(pid=f"P-{','.join(zone_ids)}", billed=False)
    for z in zone_ids:
        s.zones_visited.add(z)
    return s


def test_dead_zone_fires_when_zone_zero_others_active() -> None:
    sessions = [
        _session_in(["Z_FOH", "Z_NORTH_AISLE"]) for _ in range(6)
    ]
    out = detect_dead_zone(
        sessions,
        store_id="ST1008",
        expected_zones=["Z_FOH", "Z_NORTH_AISLE", "Z_PMU"],
        window=(T0, T0 + timedelta(hours=1)),
    )
    [a] = out
    assert a.type == AnomalyType.DEAD_ZONE
    assert a.details["zone_id"] == "Z_PMU"


def test_dead_zone_suppressed_on_quiet_window() -> None:
    sessions = [_session_in(["Z_FOH"]) for _ in range(2)]   # < min_other_visits=5
    out = detect_dead_zone(
        sessions,
        store_id="ST1008",
        expected_zones=["Z_FOH", "Z_PMU"],
        window=(T0, T0 + timedelta(hours=1)),
    )
    assert out == []


def test_dead_zone_excludes_staff() -> None:
    """Staff visits to Z_PMU shouldn't save the zone from being 'dead'."""
    customer_sessions = [_session_in(["Z_FOH"]) for _ in range(6)]
    staff_session = _session(pid="STAFF-1", billed=False, staff=True)
    staff_session.zones_visited.add("Z_PMU")

    out = detect_dead_zone(
        customer_sessions + [staff_session],
        store_id="ST1008",
        expected_zones=["Z_FOH", "Z_PMU"],
        window=(T0, T0 + timedelta(hours=1)),
    )
    [a] = out
    assert a.details["zone_id"] == "Z_PMU"
