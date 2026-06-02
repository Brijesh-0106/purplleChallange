"""Pipeline CLI — wires a detector → event builder → emitter.

Usage examples:

    # Synthetic demo — zero infra (no torch, no video, no API needed):
    python -m pipeline.run --detector synthetic --dry-run

    # Synthetic, post to a running API:
    python -m pipeline.run --detector synthetic \
        --api-url http://localhost:8000 \
        --store STORE_DEMO_001

    # Real video (Batch 4 path — needs `pip install -r requirements-pipeline.txt`):
    python -m pipeline.run --detector yolo \
        --video data/clips/STORE_BLR_002/CAM_FLOOR_01/clip.mp4 \
        --camera CAM_FLOOR_01 \
        --store STORE_BLR_002 \
        --layout data/store_layout.json \
        --api-url http://localhost:8000

The CLI is intentionally thin — it parses args, picks a detector, loads a
layout, and runs `event_builder.process_stream`. All real work is in the
modules it composes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.core.logging import configure_logging, get_logger
from app.domain.zones import StoreLayout
from app.infra.loaders.layout_loader import load_store_layouts
from pipeline.demo_layout import demo_layout
from pipeline.detector import DetectorBackend
from pipeline.emitter import DryRunEmitter, Emitter, HttpEmitter
from pipeline.event_builder import EventBuilder, EventBuilderConfig
from pipeline.layouts import BRIGADE_STORE_ID
from pipeline.reid import ReIDIndex
from pipeline.staff_classifier import LabeledStaffClassifier
from pipeline.synthetic_backend import SyntheticBackend, demo_scenario


# ─────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pipeline.run",
        description="Apex Retail · Detection pipeline runner",
    )

    p.add_argument(
        "--detector",
        choices=("synthetic", "yolo"),
        default="synthetic",
        help="Backend to use. 'synthetic' needs no extra deps.",
    )
    p.add_argument("--store", default=BRIGADE_STORE_ID)
    p.add_argument("--camera", default="CAM_FLOOR_01")

    # Layout
    p.add_argument(
        "--layout",
        type=Path,
        default=None,
        help="Path to store_layout.json. Falls back to a built-in demo layout.",
    )

    # Synthetic-specific
    p.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="ISO-8601 UTC start for synthetic / yolo scenarios.",
    )

    # YOLO-specific
    p.add_argument("--video", type=Path, default=None, help="Path to .mp4 (yolo mode).")
    p.add_argument("--weights", default="yolov8n.pt")
    p.add_argument("--device", default=None)

    # Emitter
    p.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Base URL of the Intelligence API.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print events to stdout and skip POSTing.",
    )
    p.add_argument(
        "--batch-size", type=int, default=200, help="Events per /events/ingest call."
    )

    # Event builder tuning
    p.add_argument("--min-dwell-s", type=float, default=2.0)
    p.add_argument("--track-timeout-s", type=float, default=5.0)
    p.add_argument(
        "--no-reid",
        action="store_true",
        help="Disable Re-ID (REENTRY events). Useful for A/B comparisons.",
    )

    return p.parse_args(argv)


# ─────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────


def _build_detector(args: argparse.Namespace) -> tuple[DetectorBackend, datetime | None]:
    start = _parse_start(args.start_time)
    if args.detector == "synthetic":
        backend = SyntheticBackend()
        backend.add(demo_scenario(camera_id=args.camera, start=start))
        return backend, start
    if args.detector == "yolo":
        if args.video is None:
            raise SystemExit("--video is required when --detector yolo")
        # Lazy import keeps torch out of the "synthetic" path.
        from pipeline.yolo_backend import YoloBackend

        return (
            YoloBackend(
                video_path=args.video,
                camera_id=args.camera,
                weights=args.weights,
                device=args.device,
                start_time=start,
            ),
            start,
        )
    raise SystemExit(f"unknown detector: {args.detector}")


def _build_layout(args: argparse.Namespace) -> StoreLayout:
    if args.layout is None:
        return demo_layout(store_id=args.store)
    layouts = load_store_layouts(args.layout)
    if args.store not in layouts:
        raise SystemExit(
            f"store_id {args.store!r} not in {args.layout} (have: {sorted(layouts)})"
        )
    return layouts[args.store]


def _build_emitter(args: argparse.Namespace) -> Emitter:
    if args.dry_run:
        return DryRunEmitter(print_to_stdout=True)
    return HttpEmitter(base_url=args.api_url, batch_size=args.batch_size)


def _parse_start(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    configure_logging()
    log = get_logger("pipeline.run")

    layout = _build_layout(args)
    detector, _ = _build_detector(args)
    emitter = _build_emitter(args)

    builder = EventBuilder(
        store_id=args.store,
        layout=layout,
        config=EventBuilderConfig(
            track_timeout_s=args.track_timeout_s,
            min_dwell_s=args.min_dwell_s,
        ),
        reid=None if args.no_reid else ReIDIndex(),
        # Synthetic visitors carry ground-truth `is_staff`; for real video the
        # caller can swap in `UniformColorStaffClassifier` from
        # pipeline.staff_classifier.
        staff_classifier=LabeledStaffClassifier(),
    )

    log.info(
        "pipeline.start",
        detector=args.detector,
        store_id=args.store,
        camera_id=args.camera,
        dry_run=args.dry_run,
    )

    last_ts: datetime | None = None
    batch: list = []
    total_events = 0
    flush_at = max(1, args.batch_size // 2)  # flush periodically so demo stays "live"

    try:
        for frame in detector.frames():
            for ev in builder.process_frame(frame):
                batch.append(ev)
                total_events += 1
            last_ts = frame.timestamp
            if len(batch) >= flush_at:
                emitter.emit(batch)
                batch = []

        # End of stream — close any still-open tracks with the final ts.
        if last_ts is not None:
            for ev in builder.flush(at=last_ts):
                batch.append(ev)
                total_events += 1
        if batch:
            emitter.emit(batch)
            batch = []
        emitter.flush()
    finally:
        if isinstance(emitter, HttpEmitter):
            emitter.close()

    log.info("pipeline.done", events_emitted=total_events)

    if args.dry_run:
        # Useful summary for someone piping output through `wc -l`.
        print(json.dumps({"events_emitted": total_events}), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
