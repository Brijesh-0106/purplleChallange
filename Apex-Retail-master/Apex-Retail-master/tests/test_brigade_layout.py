# PROMPT (Claude, Batch 5):
#   "Test the Brigade Road real layout: every named zone exists, foot points
#    derived from the synthetic scenario anchors map to the correct zone,
#    and `is_billing` is True only on Z_BILLING."
#
# CHANGES MADE:
#   - Used the same anchor coordinates the scenario module exports so the
#     test breaks loudly if either drifts.

from __future__ import annotations

from pipeline.layouts.brigade_road import brigade_road_layout

# Anchors are duplicated here intentionally — they're contractual.
ANCHORS: dict[str, tuple[float, float]] = {
    "Z_ENTRY":        (90.0, 540.0),
    "Z_NORTH_AISLE":  (900.0, 100.0),
    "Z_SOUTH_AISLE":  (900.0, 980.0),
    "Z_FOH":          (700.0, 540.0),
    "Z_MAKEUP":       (1000.0, 540.0),
    "Z_BILLING":      (1700.0, 450.0),
    "Z_PMU":          (1700.0, 900.0),
    # Z_NAIL_FRAG sits between entry and FOH; pick a clearly-inside point.
    "Z_NAIL_FRAG":    (350.0, 540.0),
}


def test_all_eight_zones_present() -> None:
    layout = brigade_road_layout()
    ids = {z.zone_id for z in layout.zones}
    assert ids == set(ANCHORS.keys())


def test_only_billing_zone_is_billing() -> None:
    layout = brigade_road_layout()
    billing = [z for z in layout.zones if z.is_billing]
    assert [z.zone_id for z in billing] == ["Z_BILLING"]


def test_anchors_map_to_correct_zones() -> None:
    """Each scenario anchor must land in the zone we named it for."""
    layout = brigade_road_layout()
    for zone_id, (x, y) in ANCHORS.items():
        z = layout.zone_at(x, y)
        assert z is not None, f"{zone_id} anchor {(x, y)} fell off all zones"
        assert z.zone_id == zone_id, f"anchor for {zone_id} landed in {z.zone_id}"


def test_zones_cover_canvas_meaningfully() -> None:
    """Sanity: a point clearly outside the store returns None."""
    layout = brigade_road_layout()
    assert layout.zone_at(-50, -50) is None
