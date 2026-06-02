# PROMPT (Claude, Batch 2):
#   "Write a test that verifies every line of `data/sample_events.jsonl`
#    validates against our Pydantic Event schema. Skip with a clear message
#    if the dataset isn't on disk yet (it's gitignored)."
#
# CHANGES MADE:
#   - Made the test xfail-style: SKIP (not fail) when dataset is absent so
#     CI passes before the dataset is provisioned.
#   - Reported per-line failure with line number and partial body for fast
#     debugging (when the dataset IS present).

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.events import Event

SAMPLE = Path("data") / "sample_events.jsonl"


@pytest.mark.skipif(not SAMPLE.exists(), reason=f"{SAMPLE} not provisioned yet")
def test_sample_events_all_validate() -> None:
    failures: list[tuple[int, str]] = []
    total = 0
    with SAMPLE.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            total += 1
            try:
                Event.model_validate_json(raw)
            except Exception as exc:  # noqa: BLE001
                failures.append((lineno, f"{exc}: {raw[:160]}"))

    assert total > 0, "sample_events.jsonl exists but is empty"
    assert not failures, "Some sample events failed validation:\n" + "\n".join(
        f"  L{ln}: {msg}" for ln, msg in failures[:10]
    )


def test_event_type_set_exhaustive() -> None:
    """Sanity: the closed set in `app.domain.events` matches the brief."""
    from app.domain.events import EventType

    expected = {
        "ENTRY",
        "EXIT",
        "REENTRY",
        "ZONE_ENTER",
        "ZONE_EXIT",
        "DWELL",
        "BILLING_QUEUE_JOIN",
        "BILLING_QUEUE_ABANDON",
    }
    assert set(EventType.values()) == expected
