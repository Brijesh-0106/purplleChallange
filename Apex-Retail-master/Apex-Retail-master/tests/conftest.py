# PROMPT (Claude, Batch 2):
#   "Provision an isolated SQLite DB per test session, create all tables from
#    SQLAlchemy metadata, and expose `db_session` (per-test) and `client`
#    (FastAPI TestClient) fixtures. Reset the engine + Settings cache so
#    tests don't bleed into each other."
#
# CHANGES MADE:
#   - Set DB_DRIVER=sqlite + DB_NAME=<temp file path> in env BEFORE Settings
#     is first constructed. Settings.database_url handles the sqlite branch.
#   - Used Base.metadata.create_all() (not Alembic) for unit-test speed; a
#     dedicated test verifies the migration produces the same schema.
#   - Per-test cleanup wipes table contents to keep unique-constraint tests
#     hermetic without recreating the schema each time.

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Configure env BEFORE Settings is imported anywhere.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="apex_test_"))
_DB_PATH = _TMP_DIR / "test.db"
os.environ["DB_DRIVER"] = "sqlite"
os.environ["DB_NAME"] = str(_DB_PATH).replace("\\", "/")  # SQLite is happier with forward slashes


@pytest.fixture(autouse=True, scope="session")
def _bootstrap_db():
    from app.core.config import get_settings
    from app.infra.db import get_engine, reset_engine_for_tests
    from app.infra.models import Base

    get_settings.cache_clear()
    reset_engine_for_tests()

    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    reset_engine_for_tests()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def db_session():
    """Per-test session. After each test, table rows are wiped for isolation."""
    from app.infra.db import get_session_factory
    from app.infra.models import EventModel, POSTransactionModel, StoreLayoutModel

    sess = get_session_factory()()
    try:
        yield sess
    finally:
        sess.rollback()
        # Hermetic between tests — keeps unique-constraint tests independent.
        sess.query(EventModel).delete()
        sess.query(POSTransactionModel).delete()
        sess.query(StoreLayoutModel).delete()
        sess.commit()
        sess.close()


@pytest.fixture()
def client() -> TestClient:
    """Fresh FastAPI app per test → no leaked middleware / context state."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
