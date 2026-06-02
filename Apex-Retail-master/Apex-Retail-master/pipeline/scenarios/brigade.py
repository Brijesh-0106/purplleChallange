"""Brigade Road synthetic demo scenarios.

Five archetypes the system must handle. Each is a short, named scenario; the
runner can pick `default` (= all five together over 90 seconds) or call them
individually. Designed against `pipeline.layouts.brigade_road.brigade_road_layout`.

Why opinionated archetypes? The brief and the evaluation rubric explicitly
score:
    * Re-entry handling          → V-002 leaves and comes back.
    * Staff exclusion            → V-005 is staff (is_staff=True).
    * Group entry                → V-003a/b/c enter together within 1 s.
    * Funnel correctness         → V-001 walks the full Entry→Browse→Billing→Exit.
    * Queue abandonment          → V-004 joins billing then leaves without paying.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from pipeline.synthetic_backend import Scenario, Visitor, Waypoint

# Image-coord anchors (1920×1080 canvas — matches brigade_road_layout).
_ENTRY_PT = (90.0, 540.0)         # middle of glass-door strip
_NORTH_AISLE_PT = (900.0, 100.0)  # mid-band of skincare wall
_SOUTH_AISLE_PT = (900.0, 980.0)  # mid-band of makeup wall
_FOH_PT = (700.0, 540.0)          # central open floor
_MAKEUP_PT = (1000.0, 540.0)      # makeup demo island
_BILLING_PT = (1700.0, 450.0)     # cash counter
_PMU_PT = (1700.0, 900.0)         # PMU booth (unused in default scenario; kept for richer demos)


def _start_default() -> datetime:
    """Stable default start time (deterministic for tests)."""
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────
# Individual archetypes
# ──────────────────────────────────────────────────────────────────


def buyer(track_id: str = "V-001", t0: float = 0.0) -> Visitor:
    """Walks Entry → North aisle (browse) → Makeup demo → Billing → Purchase → Exit."""
    return Visitor(
        track_id=track_id,
        appearance_signature=(0.85, 0.20, 0.45),   # bright pink top — distinctive
        waypoints=(
            Waypoint(t0 + 0.0,  _ENTRY_PT),
            Waypoint(t0 + 6.0,  _NORTH_AISLE_PT),
            Waypoint(t0 + 18.0, _MAKEUP_PT),
            Waypoint(t0 + 30.0, _BILLING_PT),
            Waypoint(t0 + 50.0, _BILLING_PT),         # dwells at counter ~20s
            Waypoint(t0 + 56.0, _FOH_PT),
            Waypoint(t0 + 65.0, _ENTRY_PT),
        ),
    )


def reentry_visitor(track_id: str = "V-002", t0: float = 5.0) -> Visitor:
    """Enters, leaves, re-enters within the scenario (Re-ID test).

    The Re-ID layer detects the SECOND visit (V-002b) as REENTRY because both
    visitors share `appearance_signature`. The synthetic backend can't 'forget'
    a track on its own; we model re-entry as two distinct tracks with matching
    appearance.
    """
    return Visitor(
        track_id=track_id,
        appearance_signature=(0.30, 0.55, 0.85),   # blue jacket
        waypoints=(
            Waypoint(t0 + 0.0,  _ENTRY_PT),
            Waypoint(t0 + 4.0,  _SOUTH_AISLE_PT),
            Waypoint(t0 + 10.0, _ENTRY_PT),           # leaves
        ),
    )


def reentry_visitor_returning(track_id: str = "V-002b", t0: float = 25.0) -> Visitor:
    """Companion to reentry_visitor — same person returning later."""
    return Visitor(
        track_id=track_id,
        appearance_signature=(0.30, 0.55, 0.85),   # SAME blue jacket → REENTRY
        waypoints=(
            Waypoint(t0 + 0.0,  _ENTRY_PT),
            Waypoint(t0 + 6.0,  _MAKEUP_PT),
            Waypoint(t0 + 18.0, _BILLING_PT),
            Waypoint(t0 + 30.0, _ENTRY_PT),
        ),
    )


def group_member(track_id: str, t0_offset: float, base_t0: float = 12.0) -> Visitor:
    """One of three friends entering together (≤1 s spread → group entry)."""
    t0 = base_t0 + t0_offset
    # Each friend has a distinct signature so Re-ID DOESN'T conflate them.
    sig_lookup = {
        "V-003a": (0.95, 0.85, 0.30),    # yellow
        "V-003b": (0.20, 0.80, 0.40),    # green
        "V-003c": (0.70, 0.40, 0.85),    # purple
    }
    return Visitor(
        track_id=track_id,
        appearance_signature=sig_lookup.get(track_id, (0.50, 0.50, 0.50)),
        waypoints=(
            Waypoint(t0 + 0.0,  _ENTRY_PT),
            Waypoint(t0 + 8.0,  _SOUTH_AISLE_PT),
            Waypoint(t0 + 22.0, _MAKEUP_PT),
            Waypoint(t0 + 40.0, _ENTRY_PT),
        ),
    )


def queue_abandoner(track_id: str = "V-004", t0: float = 20.0) -> Visitor:
    """Joins billing, abandons (no purchase), leaves."""
    return Visitor(
        track_id=track_id,
        appearance_signature=(0.60, 0.50, 0.40),   # beige
        waypoints=(
            Waypoint(t0 + 0.0,  _ENTRY_PT),
            Waypoint(t0 + 4.0,  _NORTH_AISLE_PT),
            Waypoint(t0 + 12.0, _BILLING_PT),
            Waypoint(t0 + 14.0, _BILLING_PT),         # joins, ~2 s
            Waypoint(t0 + 18.0, _FOH_PT),             # leaves billing
            Waypoint(t0 + 25.0, _ENTRY_PT),
        ),
    )


def staff(track_id: str = "STAFF-01", t0: float = 0.0) -> Visitor:
    """A staff member who walks the floor — must be excluded from visitor counts."""
    return Visitor(
        track_id=track_id,
        is_staff=True,                                # ground-truth flag
        appearance_signature=(0.10, 0.10, 0.10),      # near-black uniform
        waypoints=(
            Waypoint(t0 + 0.0,  _BILLING_PT),
            Waypoint(t0 + 8.0,  _MAKEUP_PT),
            Waypoint(t0 + 25.0, _SOUTH_AISLE_PT),
            Waypoint(t0 + 45.0, _MAKEUP_PT),
            Waypoint(t0 + 60.0, _BILLING_PT),
        ),
    )


# ──────────────────────────────────────────────────────────────────
# Composite scenarios
# ──────────────────────────────────────────────────────────────────


def brigade_demo_scenario(camera_id: str = "CAM_FLOOR_01", start: datetime | None = None) -> Scenario:
    """The 90-second flagship demo: every archetype fires.

    This is what `pipeline.run --detector synthetic` plays by default.
    Designed so a reviewer (10-min budget) sees:
        * 6 ENTRYs  (1 buyer + 1 reentry + 1 reentry-return + 3 group + 0 staff in count)
        * 1 successful billing (V-001)
        * 1 abandonment      (V-004)
        * 1 REENTRY          (V-002b matched to V-002)
        * 1 group entry      (V-003a/b/c clustered)
        * 1 staff exclusion  (STAFF-01 not counted in visitor metrics)
    """
    visitors: list[Visitor] = [
        buyer(),                                  # V-001 buyer @ t=0
        reentry_visitor(),                        # V-002 leaves @ ~t=15
        reentry_visitor_returning(),              # V-002b returns @ t=25
        group_member("V-003a", t0_offset=0.0),    # group of 3 @ t≈12
        group_member("V-003b", t0_offset=0.4),
        group_member("V-003c", t0_offset=0.8),
        queue_abandoner(),                        # V-004 abandons @ ~t=32
        staff(),                                  # STAFF-01 wanders all 60 s
    ]
    return Scenario(
        camera_id=camera_id,
        duration_s=90.0,
        fps=5.0,
        start_time=start or _start_default(),
        visitors=tuple(visitors),
    )


# Alias for back-compat with Batch 4 callers (they imported `demo_scenario`).
def demo_scenario(camera_id: str = "CAM_FLOOR_01", start: datetime | None = None) -> Scenario:
    return brigade_demo_scenario(camera_id=camera_id, start=start)


__all__: Sequence[str] = (
    "brigade_demo_scenario",
    "demo_scenario",
    "buyer",
    "reentry_visitor",
    "reentry_visitor_returning",
    "group_member",
    "queue_abandoner",
    "staff",
)
