"""Replay a synthetic / recorded event stream into the API in 'real time'.

For the live-dashboard demo we want events to land at the API roughly when
they 'happen' so the dashboard updates progressively. The pipeline runner
(`pipeline.run`) bursts the whole 90-second scenario as fast as it can —
fine for ingest tests, terrible for a demo.

This script wraps the same generator and paces the emit so each event is
posted at `event.timestamp - first_event.timestamp` seconds after start
(divided by `--speed`). With `--speed 1.0` the demo feels real-time;
`--speed 6.0` plays the 90 s scenario in 15 s — handy when stage time is
tight.

Usage:
    python -m scripts.replay_events                                  # default: brigade scenario, speed 1x
    python -m scripts.replay_events --speed 5
    python -m scripts.replay_events --api-url http://localhost:8000
    python -m scripts.replay_events --batch-size 5 --speed 3
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

from app.core.logging import configure_logging, get_logger
from pipeline.emitter import HttpEmitter
from pipeline.event_builder import EventBuilder, EventBuilderConfig
from pipeline.layouts import BRIGADE_STORE_ID, brigade_layout
from pipeline.reid import ReIDIndex
from pipeline.scenarios.brigade import brigade_demo_scenario
from pipeline.staff_classifier import LabeledStaffClassifier
from pipeline.synthetic_backend import SyntheticBackend


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="scripts.replay_events",
        description="Replay synthetic events into the API at real-time pace.",
    )
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--store", default=BRIGADE_STORE_ID)
    p.add_argument("--camera", default="CAM_FLOOR_01")
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier (1.0 = real-time, 6.0 = 6× faster).",
    )
    p.add_argument("--batch-size", type=int, default=5,
        help="Events per /events/ingest call. Smaller = more dashboard frames.")
    args = p.parse_args(argv)

    if args.speed <= 0:
        p.error("--speed must be > 0")

    configure_logging()
    log = get_logger("scripts.replay_events")

    # Build the same pipeline the runner uses, but consume the events
    # synchronously so we can pace them ourselves.
    layout = brigade_layout(store_id=args.store) if args.store != BRIGADE_STORE_ID else brigade_layout()
    backend = SyntheticBackend(scenarios=[brigade_demo_scenario(camera_id=args.camera)])
    builder = EventBuilder(
        store_id=args.store,
        layout=layout,
        config=EventBuilderConfig(min_dwell_s=2.0, group_window_s=1.0),
        reid=ReIDIndex(),
        staff_classifier=LabeledStaffClassifier(),
    )
    emitter = HttpEmitter(base_url=args.api_url, batch_size=args.batch_size)

    log.info(
        "replay.start",
        store_id=args.store, camera_id=args.camera,
        speed=args.speed, batch_size=args.batch_size,
    )
    print(
        f"▶ replaying brigade scenario at {args.speed}× speed "
        f"into {args.api_url} (store={args.store})…",
        file=sys.stderr,
    )

    t_origin: float | None = None
    last_ts = None
    sent = 0

    try:
        for frame in backend.frames():
            for ev in builder.process_frame(frame):
                _pace(ev.timestamp, t_origin, args.speed, set_origin=lambda: None)
                # Bootstrap origin on first event we actually emit.
                if t_origin is None:
                    t_origin = (ev.timestamp.timestamp(), time.monotonic())
                emitter.emit([ev])
                sent += 1
            last_ts = frame.timestamp

        if last_ts is not None:
            for ev in builder.flush(at=last_ts + timedelta(seconds=1)):
                emitter.emit([ev])
                sent += 1
        emitter.flush()
    finally:
        emitter.close()

    print(f"✅ replayed {sent} events.", file=sys.stderr)
    return 0


def _pace(event_ts, origin, speed: float, set_origin) -> None:
    """Sleep until wall-clock catches up with the event's scheduled offset."""
    if origin is None:
        return  # first event — fall through to set_origin in the caller.
    event_origin_unix, wallclock_origin = origin
    target_offset = (event_ts.timestamp() - event_origin_unix) / speed
    elapsed = time.monotonic() - wallclock_origin
    sleep_for = target_offset - elapsed
    if sleep_for > 0:
        time.sleep(sleep_for)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
