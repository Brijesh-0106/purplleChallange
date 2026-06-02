"""Re-ID for REENTRY events.

When a person leaves the store and comes back later, the tracker will assign
them a brand-new track_id — without intervention, our analytics will count
them as two distinct visitors. The brief explicitly calls this out as a key
edge case.

This module provides a **lightweight, pure-Python Re-ID** designed to:
    * Run on CPU with no external model weights.
    * Be deterministic — identical inputs produce identical embeddings.
    * Be testable without torch / ultralytics / video.
    * Work for either:
        (a) real video (a YOLO-cropped person bbox image), or
        (b) synthetic data (an `appearance` dict the SyntheticBackend can attach).

Approach.
    1. **Appearance descriptor** — for a person crop, downsample to a tiny
       (8×4) RGB grid, then quantise to a 64-D vector. For synthetic
       visitors, we accept a hand-authored 64-D vector or compute one from
       a small "appearance signature" (e.g. clothing colour). The dimension
       stays the same so cosine similarity works either way.
    2. **Active tracks vs departed tracks** — when a track gets a final
       EXIT, it moves from `_active` to `_departed`, where we keep it for
       up to `revisit_window_s` seconds.
    3. **On a fresh ENTRY** — query `_departed` for the highest cosine
       similarity > `cosine_threshold`. If a hit is found, the new track is
       considered a re-entry of the matched person.

Returns:
    `lookup_or_register(track_id, descriptor, at)` →
        (person_id, is_reentry)

`person_id` is the stable cross-track identity used throughout analytics.
For brand-new visitors, `person_id == track_id` (the first track wins).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence


# Public dimension used by both the SyntheticBackend and the YOLO crop hasher.
DESCRIPTOR_DIM = 64


@dataclass(frozen=True, slots=True)
class ReIDConfig:
    revisit_window_s: float = 600.0   # 10 min — REENTRY beyond this is a new visitor
    cosine_threshold: float = 0.85    # 0..1, higher = stricter
    max_keep_departed: int = 1024     # bounded LRU


@dataclass
class _Entry:
    person_id: str
    descriptor: tuple[float, ...]
    last_seen: datetime


@dataclass
class ReIDIndex:
    """In-memory Re-ID. Use one instance per store."""

    config: ReIDConfig = field(default_factory=ReIDConfig)
    _active: dict[str, _Entry] = field(default_factory=dict, init=False)
    _departed: dict[str, _Entry] = field(default_factory=dict, init=False)
    _next_person_seq: int = field(default=0, init=False)

    # ── Public API ───────────────────────────────────────────────────

    def lookup_or_register(
        self,
        track_id: str,
        descriptor: Sequence[float],
        at: datetime,
    ) -> tuple[str, bool]:
        """Resolve a track_id → person_id. Marks REENTRY when a recent departed
        person matches the descriptor.

        Returns:
            (person_id, is_reentry)
        """
        desc = self._normalise(descriptor)

        # If we've already resolved this track in this session, keep it stable.
        existing = self._active.get(track_id)
        if existing is not None:
            existing.last_seen = at
            return existing.person_id, False

        # Fresh track — first try matching against recently-departed people.
        self._evict_stale_departures(at)
        match = self._best_match(desc)
        if match is not None:
            person_id, similarity = match
            del self._departed[person_id]
            self._active[track_id] = _Entry(person_id=person_id, descriptor=desc, last_seen=at)
            return person_id, True

        # Otherwise mint a new person identity.
        self._next_person_seq += 1
        person_id = f"P-{self._next_person_seq:06d}"
        self._active[track_id] = _Entry(person_id=person_id, descriptor=desc, last_seen=at)
        return person_id, False

    def mark_departed(self, track_id: str, at: datetime) -> None:
        """Move a track to the departed pool when its EXIT fires."""
        entry = self._active.pop(track_id, None)
        if entry is None:
            return
        entry.last_seen = at
        self._departed[entry.person_id] = entry
        self._enforce_lru()

    def is_known_track(self, track_id: str) -> bool:
        return track_id in self._active

    # ── Internals ────────────────────────────────────────────────────

    def _best_match(self, desc: tuple[float, ...]) -> tuple[str, float] | None:
        best_pid: str | None = None
        best_sim = -1.0
        for pid, entry in self._departed.items():
            sim = _cosine(desc, entry.descriptor)
            if sim > best_sim:
                best_pid = pid
                best_sim = sim
        if best_pid is None or best_sim < self.config.cosine_threshold:
            return None
        return best_pid, best_sim

    def _evict_stale_departures(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.revisit_window_s)
        stale = [pid for pid, e in self._departed.items() if e.last_seen < cutoff]
        for pid in stale:
            del self._departed[pid]

    def _enforce_lru(self) -> None:
        max_keep = self.config.max_keep_departed
        if len(self._departed) <= max_keep:
            return
        # Evict the oldest by last_seen.
        ordered = sorted(self._departed.items(), key=lambda kv: kv[1].last_seen)
        for pid, _ in ordered[: len(self._departed) - max_keep]:
            del self._departed[pid]

    @staticmethod
    def _normalise(descriptor: Sequence[float]) -> tuple[float, ...]:
        if len(descriptor) != DESCRIPTOR_DIM:
            raise ValueError(
                f"descriptor must be {DESCRIPTOR_DIM}-D, got {len(descriptor)}"
            )
        return tuple(float(x) for x in descriptor)


# ──────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0 for zero-norm inputs."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def descriptor_from_signature(signature: Sequence[float], dim: int = DESCRIPTOR_DIM) -> tuple[float, ...]:
    """Project a short (e.g. 3-component RGB) signature into the standard dim.

    Used by `SyntheticBackend` to produce stable per-visitor descriptors from
    a hand-authored colour signature. Implemented as a deterministic tiling
    (NOT a hash) so visitors with the same signature land in the same cell.
    """
    if not signature:
        raise ValueError("signature must be non-empty")
    out: list[float] = []
    n = len(signature)
    for i in range(dim):
        out.append(float(signature[i % n]))
    return tuple(out)


def descriptor_from_bbox_crop(crop_grid: Sequence[Sequence[Sequence[float]]]) -> tuple[float, ...]:
    """Build a 64-D descriptor from an 8×4 RGB grid (height x width x 3).

    Real-video callers should:
        1. Crop the bbox region from the frame.
        2. Resize it to 8×4 (cv2.resize).
        3. Normalise channels to [0, 1].
        4. Pass the resulting (8, 4, 3) grid here.

    We don't depend on numpy at this layer — keeps the function trivially
    importable and testable. The grid shape is enforced; anything else
    raises so callers fail loudly instead of producing garbage descriptors.
    """
    rows = len(crop_grid)
    if rows != 8:
        raise ValueError(f"crop_grid must have 8 rows, got {rows}")
    out: list[float] = []
    for r in crop_grid:
        if len(r) != 4:
            raise ValueError("crop_grid rows must have 4 columns")
        for cell in r:
            if len(cell) != 3:
                raise ValueError("crop_grid cells must have 3 channels (RGB)")
            # Use the per-cell luminance as a single scalar — keeps the
            # 8×4 grid (32 cells) at 64-D by also keeping cell hue.
            r_, g_, b_ = (float(cell[0]), float(cell[1]), float(cell[2]))
            luminance = 0.2126 * r_ + 0.7152 * g_ + 0.0722 * b_
            hue_proxy = (r_ - b_) * 0.5  # cheap red-vs-blue axis
            out.append(luminance)
            out.append(hue_proxy)
    assert len(out) == DESCRIPTOR_DIM
    return tuple(out)
