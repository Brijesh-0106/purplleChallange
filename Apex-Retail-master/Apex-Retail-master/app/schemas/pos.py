"""POS transaction schema.

Source CSV columns (per challenge spec): `store_id`, `txn_id`, `timestamp`, `basket_inr`.
We accept a few benign synonyms (`amount`, `total`) to be robust to dataset variants —
only the canonical names are written to the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class POSTransaction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    store_id: str = Field(..., min_length=1, max_length=64)
    txn_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime
    basket_inr: Decimal = Field(..., ge=0)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            # POS exports often lack tz — assume UTC and surface in logs at load time.
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
