"""SQLAlchemy engine + session factory.

We use a SYNC engine for simplicity (Batch 2). Async can be swapped in later
without touching repositories — they take a `Session` as a dependency.

The engine is module-level (created at first call to `get_engine()`) so that
unit tests can swap `DATABASE_URL` via env vars without restarting the process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Singleton engine, lazily constructed.

    SQLite databases get `check_same_thread=False` and the StaticPool / file
    selected automatically — useful for tests.
    """
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    settings = get_settings()
    url = make_url(settings.database_url)

    connect_args: dict = {}
    if url.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False

    _engine = create_engine(
        url,
        echo=False,
        pool_pre_ping=True,  # transparently reconnects after DB restarts
        future=True,
        connect_args=connect_args,
    )
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    logger.info("db.engine_created", driver=url.get_backend_name(), host=url.host)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None  # for the type checker
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager providing a transactional scope.

    Commits on clean exit, rolls back on exception, always closes.
    """
    factory = get_session_factory()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def ping() -> bool:
    """Cheap connectivity check for /health. Never raises — returns False on any error."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — health check must absorb anything
        logger.warning("db.ping_failed", error=str(exc))
        return False


def reset_engine_for_tests() -> None:
    """Test-only hook: dispose the engine so the next `get_engine()` rebuilds it
    against a freshly-set DATABASE_URL. Never call this from app code."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
