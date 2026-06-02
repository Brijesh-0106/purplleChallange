# PROMPT (Claude, Batch 4):
#   "Write a test for YoloBackend that mocks `ultralytics.YOLO` so torch is
#    never imported. Cover: (1) construction, (2) frames() yields one
#    DetectorFrame per stub Result with bbox/confidence/track_id correctly
#    extracted, (3) FileNotFoundError for missing video, (4) RuntimeError
#    when ultralytics is not importable."
#
# CHANGES MADE:
#   - Used pytest's monkeypatch + sys.modules to inject fake ultralytics + cv2
#     modules at import time of YoloBackend.frames().
#   - Used a simple namespace stub for boxes — no torch tensors required;
#     just .tolist()-able objects.

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.detector import DetectorFrame
from pipeline.yolo_backend import YoloBackend


# ── Fake ultralytics + cv2 ────────────────────────────────────────────


class _FakeTensor:
    def __init__(self, data) -> None:
        self._data = data

    def tolist(self):
        return self._data

    def int(self):
        return _FakeTensor([int(x) for x in self._data])


class _FakeBoxes:
    def __init__(self, xyxy, conf, ids, cls):
        self.xyxy = _FakeTensor(xyxy)
        self.conf = _FakeTensor(conf)
        self.id = _FakeTensor(ids) if ids is not None else None
        self.cls = _FakeTensor(cls)

    def __len__(self) -> int:
        return len(self.xyxy._data)


class _FakeResult:
    def __init__(self, boxes: _FakeBoxes | None) -> None:
        self.boxes = boxes


class _FakeYOLO:
    def __init__(self, weights: str) -> None:
        self.weights = weights

    def track(self, **kwargs):
        # Two-frame fake stream:
        #   Frame 0: one person, track id 7
        #   Frame 1: same person + a second tracked person, plus a non-person class
        yield _FakeResult(
            _FakeBoxes(
                xyxy=[[100.0, 200.0, 200.0, 600.0]],
                conf=[0.91],
                ids=[7],
                cls=[0],  # person
            )
        )
        yield _FakeResult(
            _FakeBoxes(
                xyxy=[
                    [110.0, 210.0, 210.0, 610.0],
                    [400.0, 220.0, 500.0, 580.0],
                    [300.0, 230.0, 360.0, 420.0],  # non-person → must be filtered out
                ],
                conf=[0.93, 0.84, 0.99],
                ids=[7, 8, 99],
                cls=[0, 0, 1],  # person, person, non-person
            )
        )


class _FakeVideoCapture:
    """Minimal cv2.VideoCapture stub returning fps=15.0."""

    CAP_PROP_FPS = 5
    CAP_PROP_FRAME_COUNT = 7

    def __init__(self, _path: str) -> None:
        self._opened = True

    def isOpened(self) -> bool:
        return self._opened

    def get(self, prop: int) -> float:
        return {self.CAP_PROP_FPS: 15.0, self.CAP_PROP_FRAME_COUNT: 100.0}[prop]

    def release(self) -> None:
        self._opened = False


def _install_fakes(monkeypatch) -> None:
    """Inject fake ultralytics + cv2 into sys.modules before YoloBackend imports them."""
    fake_ultralytics = types.SimpleNamespace(YOLO=_FakeYOLO)
    monkeypatch.setitem(sys.modules, "ultralytics", fake_ultralytics)

    fake_cv2 = types.SimpleNamespace(
        VideoCapture=_FakeVideoCapture,
        CAP_PROP_FPS=_FakeVideoCapture.CAP_PROP_FPS,
        CAP_PROP_FRAME_COUNT=_FakeVideoCapture.CAP_PROP_FRAME_COUNT,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)


# ── Tests ────────────────────────────────────────────────────────────


def test_yolo_frames_extracts_detections(tmp_path: Path, monkeypatch) -> None:
    _install_fakes(monkeypatch)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    backend = YoloBackend(
        video_path=video,
        camera_id="CAM_FLOOR_01",
        start_time=datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    frames = list(backend.frames())
    assert len(frames) == 2
    assert all(isinstance(f, DetectorFrame) for f in frames)
    assert frames[0].camera_id == "CAM_FLOOR_01"

    # Frame 0 — one person.
    assert len(frames[0].detections) == 1
    d0 = frames[0].detections[0]
    assert d0.track_id == "7"
    assert d0.bbox == (100.0, 200.0, 200.0, 600.0)
    assert d0.confidence == pytest.approx(0.91)

    # Frame 1 — non-person class is filtered out.
    assert len(frames[1].detections) == 2
    ids = sorted(d.track_id for d in frames[1].detections)
    assert ids == ["7", "8"]


def test_yolo_timestamps_advance_with_fps(tmp_path: Path, monkeypatch) -> None:
    _install_fakes(monkeypatch)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    start = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    frames = list(
        YoloBackend(video_path=video, camera_id="CAM_X", start_time=start).frames()
    )
    # Fake fps is 15.0 → frame 1 is 1/15 s after frame 0.
    delta = (frames[1].timestamp - frames[0].timestamp).total_seconds()
    assert delta == pytest.approx(1 / 15, abs=1e-6)
    assert frames[0].timestamp == start


def test_yolo_missing_video_raises() -> None:
    with pytest.raises(FileNotFoundError):
        list(
            YoloBackend(video_path="/no/such/file.mp4", camera_id="CAM_X").frames()
        )


def test_yolo_runtime_error_when_ultralytics_missing(tmp_path: Path, monkeypatch) -> None:
    """If ultralytics isn't installed, frames() must raise a clear RuntimeError."""
    monkeypatch.delitem(sys.modules, "ultralytics", raising=False)

    # Force any fresh import to fail.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ultralytics" or name.startswith("ultralytics."):
            raise ImportError("fake: ultralytics not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    with pytest.raises(RuntimeError, match="ultralytics is not installed"):
        list(YoloBackend(video_path=video, camera_id="CAM_X").frames())
