"""Detector backend abstraction.

A `DetectorBackend` is anything that can yield `DetectorFrame`s — a sequence
of (timestamp, [Detection, ...]) pairs. The rest of the pipeline (event
builder, emitter, CLI) doesn't care whether detections came from YOLOv8 on a
real video or from a deterministic scenario file.

This split is what lets us:
    * Test the FSM logic without torch / cv2 / model weights.
    * Demo the system with `--detector synthetic` when no clips are available.
    * Plug a different model (YOLOv9, RT-DETR, …) later without touching
      anything downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Protocol


@dataclass(frozen=True, slots=True)
class Detection:
    """One detected person on one frame.

    `track_id` is None when the backend hasn't (yet) associated this detection
    with a stable track — most backends should return tracked detections, but
    the contract permits missing IDs for raw-detector use cases.
    """

    track_id: str | None
    bbox: tuple[float, float, float, float]  # (x1, y1, x2, y2) image coords
    confidence: float                          # [0, 1]
    is_staff: bool | None = None               # populated by Batch 5 staff classifier
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def centroid(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def foot_point(self) -> tuple[float, float]:
        """Bottom-centre of the bbox — better than centroid for floor-zone mapping."""
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2)


@dataclass(frozen=True, slots=True)
class DetectorFrame:
    """A single moment in the camera's timeline."""

    timestamp: datetime          # tz-aware, UTC (enforced by EventBuilder)
    frame_index: int             # 0-based; useful for debugging
    camera_id: str
    detections: tuple[Detection, ...]


class DetectorBackend(Protocol):
    """Anything that can produce a stream of `DetectorFrame`s.

    Implementations:
      * `pipeline.synthetic_backend.SyntheticBackend` — scripted scenarios.
      * `pipeline.yolo_backend.YoloBackend`           — YOLOv8 + ByteTrack on real video.

    Backends MUST yield frames in non-decreasing timestamp order per camera.
    Backends MUST NOT raise on transient failures (e.g. one decode error mid-clip);
    they should log + skip and continue. Fatal errors (file not found, corrupt
    weights) are fine to raise — the CLI catches them at the entry point.
    """

    def frames(self) -> Iterator[DetectorFrame]:
        ...
