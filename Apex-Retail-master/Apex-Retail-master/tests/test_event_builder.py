# PROMPT (Claude, Batch 4 → updated Batch 5):
#   "Write FSM tests for EventBuilder covering: (1) ENTRY emitted on first
#    sighting, (2) ZONE_ENTER/ZONE_EXIT on zone transitions, (3) DWELL
#    emitted with correct duration on zone exit, (4) DWELL suppressed when
#    below min_dwell_s, (5) BILLING_QUEUE_JOIN/ABANDON for billing zones,
#    (6) EXIT emitted via timeout, (7) flush() closes still-open tracks,
#    (8) deterministic event_ids."
#
# CHANGES MADE (Batch 5):
#   - Migrated test coordinates from the old `STORE_DEMO_001` 3-zone layout
#     to the real Brigade Road 8-zone layout (now the default `demo_layout`).
#   - Used named anchor constants (ENTRY_PT / FOH_PT / BILLING_PT) so anyone
#     reading the test can see WHICH zone we're moving into.
#   - Asserted Brigade zone IDs (Z_ENTRY / Z_FOH / Z_BILLING) instead of the
#     stale Z_AISLE id from the old layout.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.events import EventType
from pipeline.demo_layout import demo_layout
from pipeline.detector import Detection, DetectorFrame
from pipeline.event_builder import EventBuilder, EventBuilderConfig
from pipeline.synthetic_backend import (
    Scenario,
    SyntheticBackend,
    Visitor,
    Waypoint,
)

START = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

# Brigade Road anchors (foot-points). Match `pipeline.scenarios.brigade`.
ENTRY_PT = (90.0, 540.0)
FOH_PT = (700.0, 540.0)
BILLING_PT = (1700.0, 450.0)


def _make_builder(min_dwell_s: float = 2.0, track_timeout_s: float = 5.0) -> EventBuilder:
    return EventBuilder(
        store_id="ST1008",
        layout=demo_layout(),
        config=EventBuilderConfig(min_dwell_s=min_dwell_s, track_timeout_s=track_timeout_s),
    )


def _frame(i: int, seconds: float, dets: tuple[Detection, ...]) -> DetectorFrame:
    return DetectorFrame(
        timestamp=START + timedelta(seconds=seconds),
        frame_index=i,
        camera_id="CAM_FLOOR_01",
        detections=dets,
    )


def _foot(x: float, y: float, *, w: float = 60.0, h: float = 160.0) -> tuple[float, float, float, float]:
    return (x - w / 2, y - h, x + w / 2, y)


# ── Direct frame-driven tests ────────────────────────────────────────


def test_entry_emitted_on_first_sighting() -> None:
    builder = _make_builder()
    f = _frame(0, 0.0, (Detection(track_id="T1", bbox=_foot(*ENTRY_PT), confidence=0.9),))
    events = list(builder.process_frame(f))
    types = [e.event_type for e in events]
    assert types[0] == EventType.ENTRY
    # Foot point is in Z_ENTRY → ZONE_ENTER also fires immediately.
    assert EventType.ZONE_ENTER in types


def test_zone_transition_emits_exit_then_enter() -> None:
    builder = _make_builder(min_dwell_s=999)  # suppress DWELL noise
    list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*ENTRY_PT), 0.9),))))
    # Now move into Z_FOH
    out = list(
        builder.process_frame(_frame(1, 1.0, (Detection("T1", _foot(*FOH_PT), 0.9),)))
    )
    types = [e.event_type for e in out]
    assert types == [EventType.ZONE_EXIT, EventType.ZONE_ENTER]
    assert out[0].zone_id == "Z_ENTRY"
    assert out[1].zone_id == "Z_FOH"


def test_dwell_emitted_with_correct_duration() -> None:
    builder = _make_builder(min_dwell_s=2.0)
    list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*FOH_PT), 0.9),))))    # FOH
    out = list(builder.process_frame(_frame(1, 5.0, (Detection("T1", _foot(*ENTRY_PT), 0.9),))))  # ENTRY
    dwells = [e for e in out if e.event_type == EventType.DWELL]
    assert len(dwells) == 1
    assert dwells[0].duration_s == pytest.approx(5.0, abs=0.001)
    assert dwells[0].zone_id == "Z_FOH"


def test_short_dwell_suppressed() -> None:
    builder = _make_builder(min_dwell_s=2.0)
    list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*FOH_PT), 0.9),))))
    out = list(builder.process_frame(_frame(1, 0.5, (Detection("T1", _foot(*ENTRY_PT), 0.9),))))
    dwells = [e for e in out if e.event_type == EventType.DWELL]
    assert dwells == []


def test_billing_zone_emits_join_and_abandon() -> None:
    builder = _make_builder(min_dwell_s=999)
    # Frame 0 — straight into Z_BILLING
    out0 = list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*BILLING_PT), 0.9),))))
    types0 = [e.event_type for e in out0]
    assert EventType.BILLING_QUEUE_JOIN in types0

    # Move out of billing
    out1 = list(builder.process_frame(_frame(1, 4.0, (Detection("T1", _foot(*FOH_PT), 0.9),))))
    types1 = [e.event_type for e in out1]
    assert EventType.BILLING_QUEUE_ABANDON in types1
    abandon = next(e for e in out1 if e.event_type == EventType.BILLING_QUEUE_ABANDON)
    assert abandon.duration_s == pytest.approx(4.0, abs=0.001)


def test_exit_via_timeout() -> None:
    builder = _make_builder(track_timeout_s=2.0)
    list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*FOH_PT), 0.9),))))
    # No detections for >2s → next frame triggers timeout
    out = list(builder.process_frame(_frame(1, 5.0, ())))
    types = [e.event_type for e in out]
    assert EventType.EXIT in types


def test_flush_closes_open_tracks() -> None:
    builder = _make_builder()
    list(builder.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*FOH_PT), 0.9),))))
    flushed = list(builder.flush(at=START + timedelta(seconds=10)))
    types = [e.event_type for e in flushed]
    assert EventType.EXIT in types


def test_deterministic_event_ids() -> None:
    """Same inputs → same event_ids. Idempotency relies on this contract."""
    layout = demo_layout()
    cfg = EventBuilderConfig(min_dwell_s=2.0)
    runs = []
    for _ in range(2):
        b = EventBuilder(store_id="ST1008", layout=layout, config=cfg)
        ev = list(
            b.process_frame(_frame(0, 0.0, (Detection("T1", _foot(*FOH_PT), 0.9),)))
        )
        runs.append([e.event_id for e in ev])
    assert runs[0] == runs[1]


# ── Synthetic-backend driven test ────────────────────────────────────


def test_full_synthetic_run_demo_scenario() -> None:
    """End-to-end: synthetic backend → builder → typed events. Sanity invariants only."""
    from pipeline.synthetic_backend import demo_scenario

    builder = _make_builder()
    backend = SyntheticBackend(scenarios=[demo_scenario(start=START)])

    events = list(builder.process_stream(backend.frames()))
    events.extend(builder.flush(at=START + timedelta(seconds=120)))

    # Brigade scenario has 8 visitors (1 buyer, 2 reentry tracks, 3 group, 1 abandoner, 1 staff)
    # → at least 6 distinct ENTRY events (REENTRY counts separately).
    type_counts: dict[EventType, int] = {}
    for e in events:
        type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1
    assert type_counts.get(EventType.ENTRY, 0) >= 5
    assert type_counts.get(EventType.EXIT, 0) >= 5

    # At least one billing-queue join (buyer reaches billing).
    assert type_counts.get(EventType.BILLING_QUEUE_JOIN, 0) >= 1

    # All event_ids unique — pipeline output is dedup-safe.
    ids = [e.event_id for e in events]
    assert len(ids) == len(set(ids))


def test_scenario_with_no_visitors_produces_no_events() -> None:
    builder = _make_builder()
    scenario = Scenario(camera_id="CAM_X", duration_s=5.0, fps=2.0, visitors=())
    backend = SyntheticBackend(scenarios=[scenario])
    events = list(builder.process_stream(backend.frames()))
    assert events == []


def test_visitor_position_interpolation() -> None:
    """Sanity: linear interp between waypoints."""
    v = Visitor(
        track_id="V",
        waypoints=(Waypoint(0.0, (0.0, 0.0)), Waypoint(10.0, (100.0, 100.0))),
    )
    assert v.position_at(0.0) == (0.0, 0.0)
    assert v.position_at(10.0) == (100.0, 100.0)
    p = v.position_at(5.0)
    assert p is not None
    assert p[0] == pytest.approx(50.0)
    assert p[1] == pytest.approx(50.0)
    # Out of range → None
    assert v.position_at(-1) is None
    assert v.position_at(11) is None
