"""FastAPI application factory.

Composition root. Anything that needs wiring (routers, middleware, exception
handlers, lifespan hooks) gets attached here — and only here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes_dashboard import router as dashboard_router
from app.api.routes_events import router as events_router
from app.api.routes_health import router as health_router
from app.api.routes_stores import router as stores_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    """Startup / shutdown hooks. Add DB pool init here in Batch 2."""
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()
    log.info(
        "app.startup",
        service=settings.app_name,
        version=__version__,
        env=settings.app_env,
    )
    try:
        yield
    finally:
        log.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Apex Retail · Store Intelligence API",
        description=(
            "Real-time store analytics computed from CCTV-derived events. "
            "North-star metric: offline store conversion rate."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    # Order matters: middleware first, then handlers, then routers.
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(stores_router)
    app.include_router(dashboard_router)

    # Friendly root so a curl with no path doesn't 404.
    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
        }

    return app


# Uvicorn entrypoint: `uvicorn app.main:app`
app = create_app()
