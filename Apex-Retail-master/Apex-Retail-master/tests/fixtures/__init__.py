"""Test fixtures: seed the DB from synthetic events.

Used by analytics tests that need realistic event data without standing up
the API + a pipeline runner. The functions here are pure helpers; tests
import them directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.infra.repositories.events_repo import EventRepository
from app.infra.repositories.pos_repo import POSRepository
from app.schemas.events import Event
from app.schemas.pos import POSTransaction
from pipeline.event_builder import EventBuilder, EventBuilderConfig
from pipeline.layouts import brigade_layout
from pipeline.reid import ReIDIndex
from pipeline.scenarios.brigade import brigade_demo_scenario
from pipeline.staff_classifier import LabeledStaffClassifier
from pipeline.synthetic_backend import SyntheticBackend


DEFAULT_STORE_ID = "ST1008"
DEFAULT_START = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def generate_brigade_events(
    *,
    store_id: str = DEFAULT_STORE_ID,
    start: datetime = DEFAULT_START,
) -> list[Event]:
    """Run the brigade demo scenario through EventBuilder; return the events."""
    layout = brigade_layout(store_id=store_id) if store_id != DEFAULT_STORE_ID else brigade_layout()
    backend = SyntheticBackend(scenarios=[brigade_demo_scenario(start=start)])
    builder = EventBuilder(
        store_id=store_id,
        layout=layout,
        config=EventBuilderConfig(min_dwell_s=2.0, group_window_s=1.0),
        reid=ReIDIndex(),
        staff_classifier=LabeledStaffClassifier(),
    )
    events = list(builder.process_stream(backend.frames()))
    flush_at = start + timedelta(seconds=120)
    events.extend(builder.flush(at=flush_at))
    return events


def seed_brigade_events(
    db: Session,
    *,
    store_id: str = DEFAULT_STORE_ID,
    start: datetime = DEFAULT_START,
) -> list[Event]:
    """Generate the brigade scenario AND insert it into the events table."""
    events = generate_brigade_events(store_id=store_id, start=start)
    EventRepository(db).insert_many(events)
    db.commit()
    return events


def seed_pos_for_buyer(
    db: Session,
    *,
    store_id: str = DEFAULT_STORE_ID,
    start: datetime = DEFAULT_START,
    basket_inr: float = 1499.00,
    txn_id: str = "TXN-DEMO-001",
) -> POSTransaction:
    """Seed a POS row that lines up with the buyer (V-001)'s billing window.

    The buyer is at billing approx t=30s..50s after start; we land the txn
    at t=40s so it falls inside the ±5min POS window comfortably.
    """
    txn = POSTransaction(
        store_id=store_id,
        txn_id=txn_id,
        timestamp=start + timedelta(seconds=40),
        basket_inr=Decimal(str(basket_inr)),
    )
    POSRepository(db).upsert_many([txn])
    db.commit()
    return txn
