# PROMPT (Claude, Batch 4):
#   "Smoke-test the pipeline.run CLI with --detector synthetic --dry-run.
#    Confirm it: (1) exits cleanly with code 0, (2) emits a non-zero number
#    of events to stdout, (3) every emitted line is a valid Event."
#
# CHANGES MADE:
#   - Captured stdout via capsys instead of subprocess (faster, easier).
#   - Used main() directly so the test runs in-process under the same
#     interpreter / coverage tracker.

from __future__ import annotations

import json

from app.schemas.events import Event
from pipeline.run import main


def test_pipeline_run_synthetic_dry_run(capsys) -> None:
    rc = main(["--detector", "synthetic", "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "expected at least one event on stdout"

    # Every line should round-trip through the Event schema.
    for line in out:
        Event.model_validate_json(line)
