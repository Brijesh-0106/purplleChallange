# PROMPT (Claude, Batch 6):
#   "Test correlate_purchases: (1) session within ±5min of POS row gets
#    purchase=True with basket attached, (2) no match leaves session
#    untouched, (3) staff session is never matched even if a POS row
#    coincides, (4) session that didn't reach billing is never matched,
#    (5) match comes from the right STORE only."
#
# CHANGES MADE:
#   - Verified POS transactions correlation within time bounds.
#   - Verified non-matching sessions, staff sessions, non-billing sessions, and store-specific matching.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.pos_correlation import correlate_purchases
from app.services.sessions import Session


T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _session(
    *,
    store_id="ST1008",
    person_id="P-1",
    started_seconds=0,
    duration_s=60,
    reached_billing=True,
    is_staff=False,
) -> Session:
    s = Session(
        store_id=store_id,
        person_id=person_id,
        started_at=T0 + timedelta(seconds=started_seconds),
        ended_at=T0 + timedelta(seconds=started_seconds + duration_s),
        is_staff=is_staff,
    )
    if reached_billing:
        s.billing_join_count = 1
    return s


def _pos(seconds_offset: float, *, store_id="ST1008", txn="T1", basket=999.0) -> dict:
    return {
        "txn_id": txn,
        "store_id": store_id,
        "timestamp": T0 + timedelta(seconds=seconds_offset),
        "basket_inr": basket,
    }


def test_session_within_window_marks_purchase() -> None:
    sessions = [_session()]
    pos_rows = [_pos(seconds_offset=30)]   # inside billing-zone window
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is True
    assert out[0].purchase_basket_inr == 999.0


def test_no_match_leaves_session_untouched() -> None:
    sessions = [_session()]
    # POS landed 6 min after session ended — outside ±5 min window.
    pos_rows = [_pos(seconds_offset=60 + 6 * 60)]
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is False
    assert out[0].purchase_basket_inr is None


def test_staff_never_matches() -> None:
    sessions = [_session(is_staff=True)]
    pos_rows = [_pos(seconds_offset=30)]
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is False


def test_session_without_billing_visit_never_matches() -> None:
    sessions = [_session(reached_billing=False)]
    pos_rows = [_pos(seconds_offset=30)]
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is False


def test_pos_from_another_store_is_ignored() -> None:
    sessions = [_session(store_id="ST1008")]
    pos_rows = [_pos(seconds_offset=30, store_id="ST9999")]
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is False


def test_only_first_match_in_window_used() -> None:
    """Two POS rows match; the first one wins. Slightly arbitrary but documented."""
    sessions = [_session()]
    pos_rows = [
        _pos(seconds_offset=30, txn="A", basket=500.0),
        _pos(seconds_offset=45, txn="B", basket=750.0),
    ]
    out = correlate_purchases(sessions, pos_rows, window_minutes=5)
    assert out[0].purchase is True
    assert out[0].purchase_basket_inr == 500.0
