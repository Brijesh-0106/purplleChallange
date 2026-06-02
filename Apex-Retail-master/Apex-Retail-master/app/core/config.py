"""Centralised configuration backed by environment variables / `.env`.

Single source of truth for runtime settings. Anything env-dependent must come
through `get_settings()` rather than reading `os.environ` directly — this
keeps the rest of the codebase pure and trivially testable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings (12-factor)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "apex-retail-store-intelligence"
    app_env: Literal["local", "dev", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # --- API server ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Database (used from Batch 2 onwards) ---
    db_driver: str = "postgresql+psycopg"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "apex_retail"
    db_user: str = "apex"
    db_password: str = "apex_dev_password"

    # --- Operational thresholds ---
    stale_feed_threshold_minutes: int = 10

    # --- Forward-looking knobs (consumed in later batches) ---
    event_ingest_batch_max: int = Field(default=500, ge=1, le=5000)
    session_gap_minutes: int = Field(default=10, ge=1)
    pos_correlation_window_minutes: int = Field(default=5, ge=1)

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL — composed, never hard-coded elsewhere.

        SQLite gets the file-URL form (`sqlite:///<path>`) — no host/port/auth.
        Everything else uses `driver://user:pass@host:port/db`.
        """
        if self.db_driver.startswith("sqlite"):
            # db_name carries the file path for SQLite.
            return f"sqlite:///{self.db_name}"
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — call this everywhere instead of constructing Settings()."""
    return Settings()
