# PROMPT (Claude, Batch 5):
#   "Test ReIDIndex: (1) first sighting → fresh person_id, (2) same track_id
#    seen again returns same person_id (not REENTRY), (3) departed track
#    matched by descriptor returns the same person_id with is_reentry=True,
#    (4) descriptor below threshold → fresh person, (5) revisit_window
#    expiry → fresh person, (6) descriptor dimensionality enforced."
#
# CHANGES MADE:
#   - Used `descriptor_from_signature` to keep the test vocabulary aligned
#     with what SyntheticBackend does at runtime.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.reid import (
    DESCRIPTOR_DIM,
    ReIDConfig,
    ReIDIndex,
    descriptor_from_bbox_crop,
    descriptor_from_signature,
)

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_first_sighting_mints_person_id() -> None:
    idx = ReIDIndex()
    pid, reentry = idx.lookup_or_register(
        "T-1", descriptor_from_signature((0.5, 0.5, 0.5)), T0
    )
    assert reentry is False
    assert pid.startswith("P-")


def test_same_track_id_same_person_no_reentry() -> None:
    idx = ReIDIndex()
    pid1, _ = idx.lookup_or_register("T-1", descriptor_from_signature((0.5, 0.5, 0.5)), T0)
    pid2, reentry = idx.lookup_or_register(
        "T-1", descriptor_from_signature((0.5, 0.5, 0.5)), T0 + timedelta(seconds=10)
    )
    assert pid1 == pid2
    assert reentry is False


def test_departed_match_triggers_reentry() -> None:
    idx = ReIDIndex()
    sig = (0.30, 0.55, 0.85)  # blue jacket — same as the brigade reentry archetype
    pid_a, _ = idx.lookup_or_register("T-1", descriptor_from_signature(sig), T0)
    idx.mark_departed("T-1", T0 + timedelta(seconds=10))

    # Different track_id, identical descriptor — must match.
    pid_b, reentry = idx.lookup_or_register(
        "T-2", descriptor_from_signature(sig), T0 + timedelta(seconds=120)
    )
    assert reentry is True
    assert pid_a == pid_b


def test_distant_descriptor_does_not_match() -> None:
    idx = ReIDIndex()
    pid_a, _ = idx.lookup_or_register("T-1", descriptor_from_signature((0.30, 0.55, 0.85)), T0)
    idx.mark_departed("T-1", T0 + timedelta(seconds=10))
    # Wildly different signature → cosine well below threshold.
    pid_b, reentry = idx.lookup_or_register(
        "T-2", descriptor_from_signature((-0.30, -0.55, -0.85)), T0 + timedelta(seconds=60)
    )
    assert reentry is False
    assert pid_a != pid_b


def test_revisit_window_expiry() -> None:
    idx = ReIDIndex(config=ReIDConfig(revisit_window_s=30.0))
    sig = (0.7, 0.4, 0.85)
    pid_a, _ = idx.lookup_or_register("T-1", descriptor_from_signature(sig), T0)
    idx.mark_departed("T-1", T0 + timedelta(seconds=5))

    # 60 s later → outside the window.
    pid_b, reentry = idx.lookup_or_register(
        "T-2", descriptor_from_signature(sig), T0 + timedelta(seconds=60)
    )
    assert reentry is False
    assert pid_a != pid_b


def test_descriptor_dim_validated() -> None:
    idx = ReIDIndex()
    with pytest.raises(ValueError):
        idx.lookup_or_register("T-1", (1.0, 2.0, 3.0), T0)  # 3-D not 64-D


def test_descriptor_from_bbox_crop_shape() -> None:
    grid = [[[0.5, 0.5, 0.5] for _ in range(4)] for _ in range(8)]
    desc = descriptor_from_bbox_crop(grid)
    assert len(desc) == DESCRIPTOR_DIM


def test_descriptor_from_bbox_crop_rejects_bad_shape() -> None:
    bad = [[[0.5, 0.5, 0.5] for _ in range(3)] for _ in range(8)]  # 3 cols not 4
    with pytest.raises(ValueError):
        descriptor_from_bbox_crop(bad)
