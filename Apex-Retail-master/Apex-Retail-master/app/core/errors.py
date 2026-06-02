"""Centralised exception handling.

Goals (Part C — Production Readiness):
    * Never leak raw stack traces in HTTP responses.
    * Always return a structured error envelope.
    * Log unexpected errors with full traceback for operators.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for application-level errors with a stable error code."""

    code: str = "APP_ERROR"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DependencyUnavailableError(AppError):
    """Raised when a downstream dependency (DB, etc.) is unreachable. → 503."""

    code = "DEPENDENCY_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def _sanitise_pydantic_error(err: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic v2 error dict JSON-serialisable.

    Pydantic v2 surfaces the original exception object in `ctx["error"]` for
    custom validator failures (e.g. our UTC-tz `ValueError`). FastAPI's
    JSONResponse uses Starlette's encoder which can't handle arbitrary
    Python objects — so we stringify anything non-trivial in `ctx`.
    """
    out = {k: v for k, v in err.items() if k != "ctx"}
    ctx = err.get("ctx")
    if ctx:
        safe_ctx: dict[str, Any] = {}
        for k, v in ctx.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                safe_ctx[k] = v
            else:
                # Anything else (Exception instances, types, etc.) → its repr.
                safe_ctx[k] = str(v)
        out["ctx"] = safe_ctx
    return out


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers in priority order (most specific first)."""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("app.error", code=exc.code, message=exc.message, details=exc.details)
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic v2 places the original exception instance in `ctx['error']`
        # for custom-validator failures — that's a Python object, not JSON.
        # Strip / stringify those before serialising.
        sanitised = [_sanitise_pydantic_error(e) for e in exc.errors()]
        logger.info("request.validation_error", errors=sanitised)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                "VALIDATION_ERROR",
                "Request payload failed validation.",
                {"errors": sanitised},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(f"HTTP_{exc.status_code}", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Full traceback to logs, sanitised body to clients.
        logger.exception("unhandled.exception", exc_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred."),
        )
