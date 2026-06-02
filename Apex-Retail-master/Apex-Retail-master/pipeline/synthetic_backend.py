"""Synthetic detector backend.

Produces deterministic detection streams for testing and live demos when no
real CCTV footage is available. Scenarios are declarative — a list of
"visitors" each with a path through the store. The backend interpolates
positions between waypoints at the configured frame rate.

Why deterministic?
    The same scenario produces the same events every run, which makes:
      * Tests trivially assertable.
      * Demo replays predictable.
      * Reviewers able to reproduce results.

Scenarios you can build with this:
    * Single visitor: enter → browse aisle → join billing queue → exit.
    * Group entry: 3 tracks appearing within 1 second.
    * Re-entry: same person leaves, returns 2 minutes later (Batch 5 wires Re-ID).
    * Queue abandonment: visitor enters billing zone, never reaches counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

from pipeline.detector import Detection, DetectorBackend, DetectorFrame


# ─────────────────────────────────────────────────────────────────────
# Scenario building blocks
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Waypoint:
    """A point in time and image-space the visitor should be at."""

    t_offset_s: float                 # seconds since scenario start
    point: tuple[float, float]        # (x, y) in image coords (foot point)


@dataclass(frozen=True, slots=True)
class Visitor:
    """One synthetic person — what we'd see as a single tracked person."""

    track_id: str
    waypoints: tuple[Waypoint, ...]
    bbox_size: tuple[float, float] = (60.0, 160.0)   # (w, h) — typical 1080p person
    confidence: float = 0.92
    is_staff: bool | None = None
    # 3-channel RGB signature in [0, 1] used by:
    #   * Re-ID — to recognise REENTRY of "the same person".
    #   * Staff classifier — to flag dark / uniform-coloured visitors.
    # Two visitors with the same signature are considered the same person.
    appearance_signature: tuple[float, float, float] | None = None

    def position_at(self, t_offset_s: float) -> tuple[float, float] | None:
        """Linear interpolation between waypoints. Returns None outside range."""
        if not self.waypoints:
            return None
        if t_offset_s < self.waypoints[0].t_offset_s:
            return None
        if t_offset_s > self.waypoints[-1].t_offset_s:
            return None

        # Find the bracketing waypoints.
        prev = self.waypoints[0]
        for nxt in self.waypoints[1:]:
            if prev.t_offset_s <= t_offset_s <= nxt.t_offset_s:
                span = nxt.t_offset_s - prev.t_offset_s
                if span <= 0:
                    return nxt.point
                alpha = (t_offset_s - prev.t_offset_s) / span
                return (
                    prev.point[0] + alpha * (nxt.point[0] - prev.point[0]),
                    prev.point[1] + alpha * (nxt.point[1] - prev.point[1]),
                )
            prev = nxt
        return self.waypoints[-1].point


@dataclass(frozen=True, slots=True)
class Scenario:
    """A self-contained synthetic scene: one camera, N visitors, fixed length."""

    camera_id: str
    duration_s: float
    fps: float = 5.0                  # synthetic doesn't need 15 fps; 5 is plenty
    visitors: tuple[Visitor, ...] = ()
    start_time: datetime | None = None  # default: 2026-06-01T10:00:00Z


# ─────────────────────────────────────────────────────────────────────
# Backend
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SyntheticBackend:
    """Implements `DetectorBackend` over one or more scenarios.

    Multiple scenarios run sequentially (per camera), preserving the
    "monotone timestamps per camera" invariant. To simulate multi-camera
    setups, instantiate one backend per camera and merge their `frames()`
    streams (Batch 5 will provide a merger if needed).
    """

    scenarios: list[Scenario] = field(default_factory=list)

    def add(self, scenario: Scenario) -> "SyntheticBackend":
        self.scenarios.append(scenario)
        return self

    def frames(self) -> Iterator[DetectorFrame]:
        for scenario in self.scenarios:
            yield from self._run_scenario(scenario)

    def _run_scenario(self, sc: Scenario) -> Iterator[DetectorFrame]:
        start = sc.start_time or datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        step = 1.0 / sc.fps
        n_frames = int(sc.duration_s * sc.fps) + 1

        for i in range(n_frames):
            t_offset = i * step
            ts = start + timedelta(seconds=t_offset)
            dets: list[Detection] = []
            for v in sc.visitors:
                pt = v.position_at(t_offset)
                if pt is None:
                    continue
                w, h = v.bbox_size
                fx, fy = pt
                # Foot point is the bottom-centre of the bbox.
                bbox = (fx - w / 2.0, fy - h, fx + w / 2.0, fy)
                dets.append(
                    Detection(
                        track_id=v.track_id,
                        bbox=bbox,
                        confidence=v.confidence,
                        is_staff=v.is_staff,
                        extras=(
                            {"appearance_signature": list(v.appearance_signature)}
                            if v.appearance_signature is not None
                            else {}
                        ),
                    )
                )
            yield DetectorFrame(
                timestamp=ts,
                frame_index=i,
                camera_id=sc.camera_id,
                detections=tuple(dets),
            )


# ─────────────────────────────────────────────────────────────────────
# Pre-baked demo scenario — used by the CLI when no scenario file is given
# ─────────────────────────────────────────────────────────────────────


def demo_scenario(camera_id: str = "CAM_FLOOR_01", start: datetime | None = None) -> Scenario:
    """Default demo scenario.

    Delegates to the real Brigade Road, Bangalore scenario in
    `pipeline.scenarios.brigade` so old callers (Batch 4 tests, the CLI)
    transparently use the real archetypes (buyer / re-entry / group /
    abandoner / staff) against the real 8-zone layout.
    """
    # Local import to avoid a circular ref: brigade scenario imports Visitor /
    # Waypoint / Scenario from this module.
    from pipeline.scenarios.brigade import brigade_demo_scenario

    return brigade_demo_scenario(camera_id=camera_id, start=start)
