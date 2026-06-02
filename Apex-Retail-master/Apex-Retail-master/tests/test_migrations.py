# PROMPT (Claude, Batch 2):
#   "Write a migration-vs-ORM conformance test: run Alembic upgrade head
#    against an empty SQLite DB, then assert the resulting tables and columns
#    match `Base.metadata`. Catches drift between models and migrations."
#
# CHANGES MADE:
#   - Compared table names + column names per table (types differ across
#     dialects, so type-equality checks are intentionally skipped).
#   - Used a fresh in-memory SQLite (separate from the test session DB) so
#     this test doesn't perturb other tests.

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(Path("alembic.ini").resolve()))
    cfg.set_main_option("script_location", str(Path("migrations").resolve()))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_alembic_head_matches_models(tmp_path: Path) -> None:
    pytest.importorskip("alembic")
    from alembic import command

    db_path = tmp_path / "alembic_check.db"
    db_url = f"sqlite:///{str(db_path).replace(chr(92), '/')}"

    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    insp = inspect(engine)

    from app.infra.models import Base

    expected_tables = set(Base.metadata.tables.keys())
    actual_tables = set(insp.get_table_names()) - {"alembic_version"}
    assert actual_tables == expected_tables, (expected_tables, actual_tables)

    for table_name, table in Base.metadata.tables.items():
        actual_cols = {c["name"] for c in insp.get_columns(table_name)}
        expected_cols = {c.name for c in table.columns}
        assert actual_cols == expected_cols, (
            f"column drift in {table_name}: expected {expected_cols}, got {actual_cols}"
        )
