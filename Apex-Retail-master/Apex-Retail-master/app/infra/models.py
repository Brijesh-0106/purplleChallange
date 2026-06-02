"""SQLAlchemy ORM models.

Tables:
    events             — every CCTV-derived event. Append-only, idempotent.
    pos_transactions   — POS rows loaded from CSV. Idempotent on txn_id.
    store_layouts      — stored as JSON for now (one row per store). Editable
                         out-of-band; not joined heavily.

Indexing strategy is conservative for Batch 2 (just enough to make Batch 3's
ingest fast and Batch 6's analytics queries non-pathological). We'll revisit
in Batch 6 once query patterns are concrete.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base. All ORM models inherit from this."""


class EventModel(Base):
    """Events table — one row per emitted detection event."""

    __tablename__ = "events"

    # Surrogate primary key keeps inserts cheap. event_id holds the dedup invariant.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    track_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    person_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_staff: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    group_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # bbox + extras stay in a single JSON blob — they don't drive queries.
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # When the row landed (server-side; useful for debugging late arrivals).
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        # Common analytics access path: "events for store X in time range".
        Index("ix_events_store_ts", "store_id", "timestamp"),
        # Funnel / heatmap queries scope by store + zone.
        Index("ix_events_store_zone_ts", "store_id", "zone_id", "timestamp"),
    )


class POSTransactionModel(Base):
    """POS transactions loaded from `data/pos_transactions.csv`."""

    __tablename__ = "pos_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    basket_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        # Dedup: same txn_id can't appear twice for the same store.
        UniqueConstraint("store_id", "txn_id", name="uq_pos_store_txn"),
        Index("ix_pos_store_ts", "store_id", "timestamp"),
    )


class StoreLayoutModel(Base):
    """Store-layout cache. One row per store, JSON polygons."""

    __tablename__ = "store_layouts"

    store_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
