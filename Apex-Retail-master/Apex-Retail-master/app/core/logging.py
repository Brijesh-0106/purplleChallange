"""Structured logging setup.

We use `structlog` so that every log line in production is a single JSON
object with a stable shape. In local dev we render human-friendly output.

Required fields (per challenge Part C):
    trace_id, store_id, endpoint, latency_ms, event_count, status_code

These are bound contextually by the request middleware — handlers should
just log normally and trust `contextvars` to attach them.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import get_settings


class _LazyStderrLogger:
    """structlog logger that writes to the *current* `sys.stderr` each call.

    `structlog.PrintLogger` captures `sys.stderr` at construction. Combined
    with `cache_logger_on_first_use=True`, that cached reference outlives
    test boundaries — pytest's `capsys` swaps `sys.stderr` per-test and may
    close the previous stream, which then crashes log writes from cached
    loggers (`ValueError: I/O operation on closed file`).

    Reading `sys.stderr` at write time fixes that and costs nothing.
    """

    __slots__ = ()

    def msg(self, message: str) -> None:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()

    # structlog dispatches via these names after rendering. Aliasing keeps
    # the surface compatible with `BoundLogger`.
    log = debug = info = warning = error = critical = exception = failure = msg


class _LazyStderrLoggerFactory:
    """Factory used by structlog. Args (logger name, etc.) are ignored."""

    def __call__(self, *_: object) -> _LazyStderrLogger:
        return _LazyStderrLogger()


def configure_logging() -> None:
    """Idempotently configure structlog + stdlib logging.

    Logs always go to **stderr**, never stdout. This matters for CLI tools
    (`pipeline.run --dry-run`) where stdout is the data channel — mixing log
    lines into it would corrupt downstream consumers (`| jq`, tests, etc.).
    The standard Unix convention is logs→stderr / data→stdout; we follow it.

    Call once during app startup.
    """
    settings = get_settings()

    # Bridge stdlib logging into structlog (uvicorn, sqlalchemy, etc.)
    # NOTE: basicConfig is a no-op if a handler is already attached — and we
    # deliberately don't pass `force=True` to avoid fighting whatever pytest
    # has set up. Production callers get the stderr handler on first run.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stderr,
            level=settings.log_level,
        )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.log_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        # Lazy stderr resolution — see `_LazyStderrLogger` docstring.
        logger_factory=_LazyStderrLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Prefer module name as `name`."""
    return structlog.get_logger(name)
