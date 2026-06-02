"""Store-layout primitives.

A `Zone` is a labelled polygon inside a store. Each store has its zones defined
in `data/store_layout.json`. The pipeline maps person-bbox centroids to zone IDs;
analytics services aggregate by zone.

Polygon math is intentionally implemented here (not pulled from shapely) — keeps
the domain pure and CPU-light, and the use-case is well within `O(n)` ray-casting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Zone:
    """A polygonal area within a store."""

    zone_id: str
    name: str
    polygon: tuple[tuple[float, float], ...]  # ordered (x, y) vertices, image coords
    is_billing: bool = False

    def contains(self, x: float, y: float) -> bool:
        """Ray-casting point-in-polygon. Edges count as inside."""
        return _point_in_polygon(x, y, self.polygon)


@dataclass(frozen=True, slots=True)
class StoreLayout:
    """All zones for a single store + camera→zone mapping (optional)."""

    store_id: str
    zones: tuple[Zone, ...]

    def zone_at(self, x: float, y: float) -> Zone | None:
        """First zone whose polygon contains the point, else None.

        Layout files SHOULD be authored so zones don't overlap; if they do,
        the first match wins (stable order from the JSON file).
        """
        for z in self.zones:
            if z.contains(x, y):
                return z
        return None

    def by_id(self, zone_id: str) -> Zone | None:
        for z in self.zones:
            if z.zone_id == zone_id:
                return z
        return None


def _point_in_polygon(x: float, y: float, polygon: Iterable[tuple[float, float]]) -> bool:
    """Standard even-odd rule. Treats edges as inside (`<=` on the y-test)."""
    pts = list(polygon)
    n = len(pts)
    if n < 3:
        return False

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside
