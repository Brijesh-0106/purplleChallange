"""Store-layout persistence (one JSON blob per store)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.models import StoreLayoutModel


class StoreLayoutRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, store_id: str, payload: dict) -> None:
        existing = self.session.get(StoreLayoutModel, store_id)
        now = datetime.now(timezone.utc)
        if existing is None:
            self.session.add(
                StoreLayoutModel(store_id=store_id, payload=payload, updated_at=now)
            )
        else:
            existing.payload = payload
            existing.updated_at = now
        self.session.flush()

    def get(self, store_id: str) -> dict | None:
        row = self.session.execute(
            select(StoreLayoutModel).where(StoreLayoutModel.store_id == store_id)
        ).scalar_one_or_none()
        return row.payload if row else None
