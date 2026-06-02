"""Per-track event FSM.

Translates a stream of `DetectorFrame`s into a stream of validated `Event`s.
This is the brain of the pipeline — and it's *pure* (no I/O, no HTTP, no DB).
That makes it trivially testable and lets multiple detector backends share
identical event-emission semantics.

Events emitted:
    ENTRY        — first time we see a track (and Re-ID didn't match a
                   recently-departed person).
    REENTRY      — first time we see a track AND Re-ID matched a recently-
                   departed person (Batch 5; needs `reid=ReIDIndex(...)`).
    EXIT         — track has been absent for `track_timeout_s` seconds
                   OR end-of-stream `flush()` is called.
    ZONE_ENTER   — the foot-point crosses into a different non-billing zone.
    ZONE_EXIT    — the foot-point crosses out of the previous non-billing zone.
    DWELL        — emitted on ZONE_EXIT *and* on EXIT, with the duration spent
                   in the zone the track was last seen in (configurable
                   minimum, default 2.0s, drops noise).
    BILLING_QUEUE_JOIN  — ZONE_ENTER into a zone flagged `is_billing=True`.
    BILLING_QUEUE_ABANDON — emitted instead of a normal DWELL when a billing-zone
                            track exits without a recorded purchase nearby. POS
                            correlation runs in Batch 6; for the pipeline we
                            always emit ABANDON on billing-zone exit and let
                            the analytics layer reclassify if a POS row matches.

Batch 5 additions:
    * **Re-ID for REENTRY** via an optional `ReIDIndex`. When provided, every
      new track_id is run through `lookup_or_register` so the same person
      walking back into the store after EXIT keeps a stable `person_id` and
      gets a REENTRY event instead of a fresh ENTRY.
    * **Staff classification** via an optional `StaffClassifier`. When the
      classifier flags a detection as staff, every event for that track
      carries `is_staff=True`. Analytics layer is responsible for excluding
      `is_staff=True` from visitor-count metrics.
    * **Group entry detection**. If ≥2 ENTRY events fall within
      `group_window_s` (default 1.0 s), the events get a `group_size` field
      reflecting the size of the burst.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from app.core.logging import get_logger
from app.domain.events import EventType
from app.domain.zones import StoreLayout, Zone
from app.schemas.events import Event
from pipeline.detector import Detection, DetectorFrame
from pipeline.reid import ReIDIndex, descriptor_from_signature
from pipeline.staff_classifier import StaffClassifier

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Per-track state
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _TrackState:
    """Mutable state tracked per `track_id` for the duration of a clip."""

    track_id: str
    person_id: str                       # stable identity across REENTRY
    entry_ts: datetime
    last_seen_ts: datetime
    last_camera: str
    is_staff: bool                       # locked at first sighting
    last_zone: Zone | None = None
    zone_entered_ts: datetime | None = None
    last_detection: Detection | None = None
    closed: bool = False


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EventBuilderConfig:
    """Tuning knobs. Defaults are sensible for 1080p15fps retail footage."""

    track_timeout_s: float = 5.0     # absent this long → emit EXIT
    min_dwell_s: float = 2.0         # below this → suppress DWELL emission (noise)
    event_id_prefix: str = "evt"     # prefixes the deterministic event_id
    group_window_s: float = 1.0      # ≥2 entries within this window → group


# ─────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────


@dataclass
class EventBuilder:
    """Stateful event emitter.

    Usage:

        builder = EventBuilder(
            store_id="ST1008",
            layout=brigade_layout(),
            reid=ReIDIndex(),
            staff_classifier=LabeledStaffClassifier(),
        )
        for ev in builder.process_stream(detector.frames()):
            emit(ev)
        # Always flush at end-of-stream so still-open tracks emit EXIT.
        for ev in builder.flush(at=last_seen_ts):
            emit(ev)
    """

    store_id: str
    layout: StoreLayout
    config: EventBuilderConfig = field(default_factory=EventBuilderConfig)
    reid: ReIDIndex | None = None
    staff_classifier: StaffClassifier | None = None

    # internal
    _tracks: dict[str, _TrackState] = field(default_factory=dict, init=False)
    _seq: int = field(default=0, init=False)
    # Sliding window of (timestamp, track_id) for recent ENTRYs/REENTRYs —
    # used to retroactively stamp `group_size` when a burst is detected.
    _recent_entries: list[tuple[datetime, str]] = field(default_factory=list, init=False)
    # Track-id → list of pending events that should learn their group_size
    # once the window closes. We yield them after the window-close so we
    # have the right number to stamp.
    _pending_group_events: list[Event] = field(default_factory=list, init=False)

    # ── Public API ───────────────────────────────────────────────────

    def process_stream(self, frames: Iterator[DetectorFrame]) -> Iterator[Event]:
        """Drive the FSM over a stream of frames; yield events as they happen."""
        for frame in frames:
            yield from self.process_frame(frame)

    def process_frame(self, frame: DetectorFrame) -> Iterator[Event]:
        """Process one frame; yield zero-or-more events.

        Order of operations matters: timeouts first (so an EXIT fires before a
        new ENTRY for a recycled track_id), then per-detection updates.
        """
        ts = _ensure_utc(frame.timestamp)

        # 1. Timeouts: anyone we haven't seen in `track_timeout_s` is gone.
        yield from self._sweep_timeouts(ts)

        # 2. Per-detection updates. ENTRY events for this frame are buffered
        # so we can compute group_size across all simultaneous ENTRYs in the
        # frame before yielding them.
        held_entries: list[Event] = []
        other_events: list[Event] = []

        for det in frame.detections:
            if det.track_id is None:
                continue
            for ev in self._update_track(frame.camera_id, ts, det):
                if ev.event_type in (EventType.ENTRY, EventType.REENTRY):
                    held_entries.append(ev)
                else:
                    other_events.append(ev)

        # 3. If multiple ENTRY/REENTRY events landed in this frame *or* in the
        # last `group_window_s` seconds, stamp group_size on all of them.
        yield from self._emit_with_group_size(held_entries, ts)

        # 4. Yield non-entry events (zone transitions, dwell, etc.).
        yield from other_events

    def flush(self, at: datetime) -> Iterator[Event]:
        """Close all still-open tracks (e.g. end-of-clip)."""
        ts = _ensure_utc(at)
        for state in list(self._tracks.values()):
            if state.closed:
                continue
            yield from self._close_track(state, ts)

    # ── Internals ────────────────────────────────────────────────────

    def _update_track(self, camera_id: str, ts: datetime, det: Detection) -> Iterator[Event]:
        tid = det.track_id
        assert tid is not None  # callers checked
        state = self._tracks.get(tid)

        if state is None:
            # New track — resolve identity (Re-ID), staff status, then emit
            # ENTRY or REENTRY accordingly.
            person_id, is_reentry = self._resolve_identity(tid, det, ts)
            is_staff = self._classify_staff(det)
            state = _TrackState(
                track_id=tid,
                person_id=person_id,
                entry_ts=ts,
                last_seen_ts=ts,
                last_camera=camera_id,
                is_staff=is_staff,
                last_detection=det,
            )
            self._tracks[tid] = state
            yield self._build_entry(state, det, ts, reentry=is_reentry)
            # Fall through so a same-frame zone-enter also fires.

        # Always refresh last-seen.
        state.last_seen_ts = ts
        state.last_camera = camera_id
        state.last_detection = det

        # Zone transitions — based on foot point.
        new_zone = self.layout.zone_at(*det.foot_point)
        old_zone = state.last_zone

        if new_zone is None and old_zone is None:
            return  # still off-zone; nothing to emit

        if new_zone is None and old_zone is not None:
            yield from self._emit_zone_exit(state, old_zone, ts, det)
            state.last_zone = None
            state.zone_entered_ts = None
            return

        if old_zone is None and new_zone is not None:
            yield self._emit_zone_enter(state, new_zone, ts, det)
            state.last_zone = new_zone
            state.zone_entered_ts = ts
            return

        if new_zone is not None and old_zone is not None and new_zone.zone_id != old_zone.zone_id:
            yield from self._emit_zone_exit(state, old_zone, ts, det)
            yield self._emit_zone_enter(state, new_zone, ts, det)
            state.last_zone = new_zone
            state.zone_entered_ts = ts

    def _sweep_timeouts(self, now: datetime) -> Iterator[Event]:
        timeout = self.config.track_timeout_s
        for state in list(self._tracks.values()):
            if state.closed:
                continue
            if (now - state.last_seen_ts).total_seconds() > timeout:
                yield from self._close_track(state, state.last_seen_ts)

    def _close_track(self, state: _TrackState, ts: datetime) -> Iterator[Event]:
        # Final ZONE_EXIT/DWELL if still inside a zone.
        if state.last_zone is not None and state.zone_entered_ts is not None:
            yield from self._emit_zone_exit(state, state.last_zone, ts, state.last_detection)
            state.last_zone = None
            state.zone_entered_ts = None

        yield self._build_exit(state, ts)
        # Notify Re-ID so a future track sharing this person's appearance
        # is recognised as REENTRY.
        if self.reid is not None:
            self.reid.mark_departed(state.track_id, ts)
        state.closed = True

    # ── Identity / staff resolution ──────────────────────────────────

    def _resolve_identity(
        self, track_id: str, det: Detection, ts: datetime
    ) -> tuple[str, bool]:
        """Return (person_id, is_reentry)."""
        if self.reid is None:
            return track_id, False

        descriptor = self._descriptor_for(det)
        if descriptor is None:
            # No descriptor available → can't Re-ID; treat as fresh.
            return track_id, False

        return self.reid.lookup_or_register(track_id, descriptor, ts)

    def _classify_staff(self, det: Detection) -> bool:
        if self.staff_classifier is None:
            return bool(det.is_staff) if det.is_staff is not None else False
        return self.staff_classifier.is_staff(det)

    def _descriptor_for(self, det: Detection):
        """Pull a Re-ID descriptor from the detection's extras.

        SyntheticBackend attaches `appearance_signature` (a 3-component RGB
        triple); the YOLO backend would attach a pre-computed crop descriptor
        directly. We accept either:
            * a 64-D `descriptor` directly, OR
            * a short `appearance_signature` we project to 64-D.
        """
        if "descriptor" in det.extras:
            return tuple(det.extras["descriptor"])
        sig = det.extras.get("appearance_signature")
        if sig is None:
            return None
        return descriptor_from_signature(sig)

    # ── Group entry stamping ─────────────────────────────────────────

    def _emit_with_group_size(
        self, entries_this_frame: list[Event], ts: datetime
    ) -> Iterator[Event]:
        """Stamp `group_size` on ENTRY/REENTRY events that arrived in a burst.

        The group is the union of:
          * all ENTRYs in *this* frame, plus
          * any ENTRYs in `_recent_entries` that are within `group_window_s`.

        Implementation note: only the events from this frame can be stamped
        post-hoc here. Earlier events that landed in prior frames have already
        been yielded. To keep things simple and observable, we set
        `group_size` on the current-frame events using the *combined* count.
        That gives analytics a reliable signal: any ENTRY with
        `group_size >= 2` is part of a burst.
        """
        # Drop entries older than the window.
        cutoff = ts.timestamp() - self.config.group_window_s
        self._recent_entries = [
            (t, tid) for (t, tid) in self._recent_entries if t.timestamp() >= cutoff
        ]

        if not entries_this_frame:
            return

        # Update the window with this frame's entries.
        for ev in entries_this_frame:
            self._recent_entries.append((ts, ev.track_id or ""))

        combined = len(self._recent_entries)
        if combined >= 2:
            for ev in entries_this_frame:
                # Pydantic models are immutable by default but we created
                # them with `extra='allow'`; use model_copy to attach
                # group_size cleanly.
                yield ev.model_copy(update={"group_size": combined})
        else:
            yield from entries_this_frame

    # ── Event factories ──────────────────────────────────────────────

    def _build_entry(
        self, state: _TrackState, det: Detection, ts: datetime, *, reentry: bool
    ) -> Event:
        et = EventType.REENTRY if reentry else EventType.ENTRY
        return Event.model_validate(
            {
                "event_id": self._eid(state.track_id, et.value, ts),
                "event_type": et.value,
                "store_id": self.store_id,
                "camera_id": state.last_camera,
                "timestamp": ts,
                "track_id": state.track_id,
                "person_id": state.person_id,
                "confidence": det.confidence,
                "bbox": list(det.bbox),
                "is_staff": state.is_staff,
            }
        )

    def _build_exit(self, state: _TrackState, ts: datetime) -> Event:
        return Event.model_validate(
            {
                "event_id": self._eid(state.track_id, "EXIT", ts),
                "event_type": EventType.EXIT.value,
                "store_id": self.store_id,
                "camera_id": state.last_camera,
                "timestamp": ts,
                "track_id": state.track_id,
                "person_id": state.person_id,
                "confidence": state.last_detection.confidence if state.last_detection else None,
                "is_staff": state.is_staff,
            }
        )

    def _emit_zone_enter(
        self, state: _TrackState, zone: Zone, ts: datetime, det: Detection
    ) -> Event:
        et = EventType.BILLING_QUEUE_JOIN if zone.is_billing else EventType.ZONE_ENTER
        if et == EventType.BILLING_QUEUE_JOIN:
            duration_s: float | None = 0.0
        else:
            duration_s = None

        return Event.model_validate(
            {
                "event_id": self._eid(state.track_id, et.value, ts, zone.zone_id),
                "event_type": et.value,
                "store_id": self.store_id,
                "camera_id": state.last_camera,
                "timestamp": ts,
                "track_id": state.track_id,
                "person_id": state.person_id,
                "zone_id": zone.zone_id,
                "duration_s": duration_s,
                "confidence": det.confidence,
                "bbox": list(det.bbox),
                "is_staff": state.is_staff,
            }
        )

    def _emit_zone_exit(
        self,
        state: _TrackState,
        zone: Zone,
        ts: datetime,
        det: Detection | None,
    ) -> Iterator[Event]:
        assert state.zone_entered_ts is not None
        duration = max((ts - state.zone_entered_ts).total_seconds(), 0.0)

        if zone.is_billing:
            yield Event.model_validate(
                {
                    "event_id": self._eid(state.track_id, "BILLING_QUEUE_ABANDON", ts, zone.zone_id),
                    "event_type": EventType.BILLING_QUEUE_ABANDON.value,
                    "store_id": self.store_id,
                    "camera_id": state.last_camera,
                    "timestamp": ts,
                    "track_id": state.track_id,
                    "person_id": state.person_id,
                    "zone_id": zone.zone_id,
                    "duration_s": duration,
                    "confidence": det.confidence if det else None,
                    "is_staff": state.is_staff,
                }
            )
        else:
            yield Event.model_validate(
                {
                    "event_id": self._eid(state.track_id, "ZONE_EXIT", ts, zone.zone_id),
                    "event_type": EventType.ZONE_EXIT.value,
                    "store_id": self.store_id,
                    "camera_id": state.last_camera,
                    "timestamp": ts,
                    "track_id": state.track_id,
                    "person_id": state.person_id,
                    "zone_id": zone.zone_id,
                    "confidence": det.confidence if det else None,
                    "is_staff": state.is_staff,
                }
            )

        if duration >= self.config.min_dwell_s:
            yield Event.model_validate(
                {
                    "event_id": self._eid(state.track_id, "DWELL", ts, zone.zone_id),
                    "event_type": EventType.DWELL.value,
                    "store_id": self.store_id,
                    "camera_id": state.last_camera,
                    "timestamp": ts,
                    "track_id": state.track_id,
                    "person_id": state.person_id,
                    "zone_id": zone.zone_id,
                    "duration_s": duration,
                    "confidence": det.confidence if det else None,
                    "is_staff": state.is_staff,
                }
            )

    def _eid(self, track_id: str, kind: str, ts: datetime, zone_id: str | None = None) -> str:
        """Deterministic event_id.

        Format: <prefix>:<store>:<track>:<kind>[:<zone>]:<unix_ms>:<seq>
        """
        self._seq += 1
        unix_ms = int(ts.timestamp() * 1000)
        parts = [self.config.event_id_prefix, self.store_id, track_id, kind]
        if zone_id is not None:
            parts.append(zone_id)
        parts.extend([str(unix_ms), str(self._seq)])
        return ":".join(parts)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
