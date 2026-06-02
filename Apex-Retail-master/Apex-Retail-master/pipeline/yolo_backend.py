"""YOLOv8 + ByteTrack backend.

Wraps Ultralytics' `YOLO(...).track()` into our `DetectorBackend` protocol.
The torch / cv2 / ultralytics imports are deliberately deferred to first
construction so that:

    * `pytest` never imports torch (saves ~200MB of resolution + ~3s startup).
    * The synthetic-only demo path on a Lilly machine without ultralytics
      installed still works.

Install on demand:
    pip install -r requirements-pipeline.txt

Model weights:
    Ultralytics auto-downloads `yolov8n.pt` (~6 MB) on first use, caching
    it in `~/.cache/ultralytics/`. If the corporate proxy blocks GitHub /
    HuggingFace, pre-download the .pt file and pass `--weights /path/to/yolov8n.pt`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.core.logging import get_logger
from pipeline.detector import Detection, DetectorBackend, DetectorFrame

logger = get_logger(__name__)


@dataclass
class YoloBackend:
    """Real-video detection backend.

    Args:
        video_path: path to the .mp4 / .avi clip.
        camera_id:  carried verbatim into emitted events.
        weights:    YOLO weights path or short name. Default: 'yolov8n.pt'.
        tracker:    Ultralytics tracker config name. Default: 'bytetrack.yaml'.
        conf:       Min confidence (0..1). Detections below are dropped.
        iou:        NMS IoU threshold.
        device:     'cpu' | 'cuda:0' | None (auto).
        start_time: clip wall-clock start (UTC). Frames timestamps derive
                    from `start_time + frame_index/fps`. None → file's
                    creation time, fallback to "now" if unreadable.
        person_class_id: COCO class index for "person" (= 0 in COCO).
    """

    video_path: str | Path
    camera_id: str
    weights: str = "yolov8n.pt"
    tracker: str = "bytetrack.yaml"
    conf: float = 0.4
    iou: float = 0.5
    device: str | None = None
    start_time: datetime | None = None
    person_class_id: int = 0

    # ── Public API ───────────────────────────────────────────────────

    def frames(self) -> Iterator[DetectorFrame]:
        """Yield `DetectorFrame`s for every successfully-decoded frame."""
        path = Path(self.video_path)
        if not path.exists():
            raise FileNotFoundError(f"video not found: {path}")

        # Lazy imports — see module docstring.
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as exc:  # pragma: no cover — surfaces in CLI
            raise RuntimeError(
                "ultralytics is not installed. Run "
                "`pip install -r requirements-pipeline.txt` "
                "or use `--detector synthetic`."
            ) from exc

        fps, total = self._probe_video(str(path))
        start = self._resolve_start_time(path)
        logger.info(
            "yolo.start",
            path=str(path),
            camera_id=self.camera_id,
            fps=fps,
            total_frames=total,
            weights=self.weights,
        )

        model = YOLO(self.weights)

        # `stream=True` yields per-frame Results, doesn't load the whole clip.
        results = model.track(
            source=str(path),
            stream=True,
            persist=True,
            tracker=self.tracker,
            classes=[self.person_class_id],
            conf=self.conf,
            iou=self.iou,
            device=self.device,
            verbose=False,
        )

        for i, res in enumerate(results):
            ts = start + timedelta(seconds=i / fps if fps > 0 else 0.0)
            yield DetectorFrame(
                timestamp=ts,
                frame_index=i,
                camera_id=self.camera_id,
                detections=tuple(self._extract_detections(res)),
            )

    # ── Internals ────────────────────────────────────────────────────

    def _extract_detections(self, res) -> Iterator[Detection]:  # noqa: ANN001
        """Pull Detection objects out of an Ultralytics Results object."""
        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return

        # Each of these is a tensor; .tolist() is cheap and avoids a numpy
        # dependency at this layer.
        xyxy = boxes.xyxy.tolist()
        confs = boxes.conf.tolist()
        ids = (
            boxes.id.int().tolist()
            if getattr(boxes, "id", None) is not None
            else [None] * len(xyxy)
        )
        clses = boxes.cls.int().tolist() if getattr(boxes, "cls", None) is not None else []

        for i, (bbox, cf) in enumerate(zip(xyxy, confs, strict=False)):
            cls = clses[i] if i < len(clses) else None
            if cls is not None and cls != self.person_class_id:
                continue
            tid = ids[i] if i < len(ids) and ids[i] is not None else None
            yield Detection(
                track_id=str(tid) if tid is not None else None,
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                confidence=float(cf),
            )

    def _probe_video(self, path: str) -> tuple[float, int]:
        """Return (fps, total_frames). Falls back to (15.0, 0) if cv2 fails."""
        try:
            import cv2  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "opencv-python is not installed. Run "
                "`pip install -r requirements-pipeline.txt`."
            ) from exc

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return 15.0, 0
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 15.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        return fps, total

    def _resolve_start_time(self, path: Path) -> datetime:
        """Best-effort wall-clock start.

        Order of preference:
            1. Caller-supplied `self.start_time`.
            2. File's mtime — for archived footage this is a decent proxy.
            3. Now (UTC) — fallback so we never produce naive timestamps.
        """
        if self.start_time is not None:
            return (
                self.start_time.astimezone(timezone.utc)
                if self.start_time.tzinfo
                else self.start_time.replace(tzinfo=timezone.utc)
            )
        try:
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc)
        except OSError:
            return datetime.now(tz=timezone.utc)
