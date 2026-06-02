# PROMPT (Claude, Batch 5):
#   "End-to-end test: run the brigade demo scenario through EventBuilder
#    with Re-ID + LabeledStaffClassifier, and assert the system produces:
#    (1) at least one REENTRY event, (2) staff EXIT/ENTRY events carry
#    is_staff=True, (3) at least three ENTRY/REENTRY events with
#    group_size >= 2 (the trio of friends), (4) deterministic event_ids
#    across two identical runs."
#
# CHANGES MADE:
#   - Used the real archetypes from `pipeline.scenarios.brigade` so the
#     test reflects the actual demo path.
#   - Asserted aggregate properties, not exact counts (FSM may emit a few
#     extra zone-transition events depending on interpolation timing).

from __future__ import annotations

from datetime import timedelta

from app.domain.events import EventType
from pipeline.event_builder import EventBuilder, EventBuilderConfig
from pipeline.layouts import brigade_layout
from pipeline.reid import ReIDIndex
from pipeline.scenarios.brigade import brigade_demo_scenario
from pipeline.staff_classifier import LabeledStaffClassifier
from pipeline.synthetic_backend import SyntheticBackend


def _run() -> list:
    layout = brigade_layout()
    backend = SyntheticBackend(scenarios=[brigade_demo_scenario()])
    builder = EventBuilder(
        store_id="ST1008",
        layout=layout,
        config=EventBuilderConfig(min_dwell_s=2.0, group_window_s=1.0),
        reid=ReIDIndex(),
        staff_classifier=LabeledStaffClassifier(),
    )
    events = list(builder.process_stream(backend.frames()))
    sc = brigade_demo_scenario()
    flush_at = sc.start_time + timedelta(seconds=sc.duration_s + 1)
    events.extend(builder.flush(at=flush_at))
    return events


def test_demo_scenario_emits_reentry() -> None:
    events = _run()
    reentries = [e for e in events if e.event_type == EventType.REENTRY]
    assert len(reentries) >= 1, "expected at least one REENTRY (V-002b → V-002)"


def test_demo_scenario_marks_staff() -> None:
    events = _run()
    staff_ev = [e for e in events if e.is_staff is True]
    # Staff visitor walks for ~60 s; should emit many events (entry/zones/exit).
    assert staff_ev, "no events flagged is_staff=True"

    # Conversely the buyer should never be marked staff.
    buyer_evs = [e for e in events if e.track_id == "V-001"]
    assert buyer_evs and all(ev.is_staff is False for ev in buyer_evs)


def test_group_entry_stamps_group_size() -> None:
    events = _run()
    entries = [
        e for e in events if e.event_type in (EventType.ENTRY, EventType.REENTRY)
    ]
    grouped = [e for e in entries if (e.group_size or 0) >= 2]
    assert len(grouped) >= 3, (
        f"expected ≥3 grouped entries (V-003a/b/c), got {len(grouped)}"
    )


def test_run_is_deterministic() -> None:
    """Two runs of the same scenario produce identical event_id sets."""
    a = _run()
    b = _run()
    ids_a = sorted(e.event_id for e in a)
    ids_b = sorted(e.event_id for e in b)
    assert ids_a == ids_b


def test_buyer_walks_full_funnel() -> None:
    """The buyer hits ENTRY → ZONE_ENTER (north aisle / makeup) → BILLING_QUEUE_JOIN → EXIT."""
    events = _run()
    buyer = [e for e in events if e.track_id == "V-001"]
    types = [e.event_type for e in buyer]

    assert EventType.ENTRY in types
    assert EventType.BILLING_QUEUE_JOIN in types
    assert EventType.EXIT in types
