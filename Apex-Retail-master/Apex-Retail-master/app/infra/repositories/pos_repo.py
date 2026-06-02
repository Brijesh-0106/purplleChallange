"""POS persistence.

Idempotency: same (`store_id`, `txn_id`) pair never produces a duplicate row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.infra.models import POSTransactionModel
from app.schemas.pos import POSTransaction


class POSRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, txns: list[POSTransaction]) -> tuple[int, int]:
        """Insert any new (store_id, txn_id) pairs. Returns (inserted, skipped)."""
        if not txns:
            return 0, 0

        keys = [(t.store_id, t.txn_id) for t in txns]
        existing = set(
            self.session.execute(
                select(POSTransactionModel.store_id, POSTransactionModel.txn_id).where(
                    tuple_(POSTransactionModel.store_id, POSTransactionModel.txn_id).in_(keys)
                )
            ).all()
        )

        inserted = 0
        skipped = 0
        for t in txns:
            if (t.store_id, t.txn_id) in existing:
                skipped += 1
                continue
            self.session.add(
                POSTransactionModel(
                    store_id=t.store_id,
                    txn_id=t.txn_id,
                    timestamp=t.timestamp,
                    basket_inr=t.basket_inr,
                )
            )
            inserted += 1

        self.session.flush()
        return inserted, skipped

    def count_for_store(self, store_id: str) -> int:
        from sqlalchemy import func

        return int(
            self.session.execute(
                select(func.count(POSTransactionModel.id)).where(
                    POSTransactionModel.store_id == store_id
                )
            ).scalar_one()
        )

    def fetch_in_window(
        self,
        store_id: str,
        since,
        until,
    ) -> list[dict]:
        """Return POS transactions for a store within [since, until]."""
        from sqlalchemy import asc

        rows = self.session.execute(
            select(
                POSTransactionModel.txn_id,
                POSTransactionModel.store_id,
                POSTransactionModel.timestamp,
                POSTransactionModel.basket_inr,
            )
            .where(POSTransactionModel.store_id == store_id)
            .where(POSTransactionModel.timestamp >= since)
            .where(POSTransactionModel.timestamp <= until)
            .order_by(asc(POSTransactionModel.timestamp))
        ).all()
        return [
            {
                "txn_id": r.txn_id,
                "store_id": r.store_id,
                "timestamp": _utc(r.timestamp),
                "basket_inr": float(r.basket_inr),
            }
            for r in rows
        ]


def _utc(ts: datetime | None) -> datetime | None:
    """Tag a naive datetime as UTC. Idempotent for already-aware values.

    SQLite drops tzinfo from `DateTime(timezone=True)` columns; Postgres
    preserves it. Normalising on read keeps every downstream timestamp
    comparable without per-call defensive code.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
