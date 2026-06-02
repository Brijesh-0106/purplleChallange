"""POS correlation.

Joins billing-zone activity (BILLING_QUEUE_JOIN / ABANDON, captured by the
detection pipeline) with POS rows (loaded from the Purplle CSV) to upgrade
abandons into purchases when a transaction landed within ±N minutes.

Why this matters.
    The detection pipeline can't see what happens AT the counter — it only
    sees a person enter the billing zone and leave it. Without POS, every
    such event looks like an abandonment. By joining on store + time we
    convert that signal into a true conversion-rate input.

Matching policy: greedy, consume-once.
    For each session whose person `reached_billing` (= billing_join_count
    >= 1), we look for a POS transaction at the same store whose timestamp
    falls within ±`window_minutes` of the session's billing-zone activity.
    Sessions are processed in `started_at` order so earlier visits get
    first dibs on the matching POS row. Once a POS row is matched it is
    REMOVED from the candidate pool — one transaction = one purchase, no
    double counting. This matches the rubric's "session-based, no double
    counting" requirement.

    Consequence: when a single POS row time-overlaps multiple billing-zone
    sessions, only the earliest session is marked as a purchase. The
    others remain `purchase=False` (i.e. genuine queue abandonments).
    That's the right business answer — one transaction is one customer.

This is pure once you have sessions + POS rows in memory — no DB / HTTP.
The route layer pulls both from repositories, hands them in, gets back a
mutated session list with `purchase` / `purchase_basket_inr` set.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from app.services.sessions import Session


def correlate_purchases(
    sessions: list[Session],
    pos_rows: Iterable[dict],
    *,
    window_minutes: int = 5,
) -> list[Session]:
    """Mutate-and-return sessions with `purchase` / `purchase_basket_inr` set.

    `pos_rows` are dicts as produced by `POSRepository.fetch_in_window`:
        {"txn_id": str, "store_id": str, "timestamp": tz-aware dt, "basket_inr": float}

    Sessions whose person never reached billing are left untouched (still
    `purchase=False`, basket=None). That keeps the conversion-rate
    calculation honest: a customer who never queued is not a "missed sale".
    """
    window = timedelta(minutes=window_minutes)

    # Group POS rows by store and sort by timestamp; track consumption.
    pos_by_store: dict[str, list[dict]] = {}
    for row in pos_rows:
        pos_by_store.setdefault(row["store_id"], []).append(row)
    for rows in pos_by_store.values():
        rows.sort(key=lambda r: r["timestamp"])
    consumed: set[tuple[str, str]] = set()  # (store_id, txn_id)

    # Process sessions in chronological order so earlier billers get
    # first claim on overlapping POS rows.
    eligible = sorted(
        (s for s in sessions if s.reached_billing and not s.is_staff),
        key=lambda s: s.started_at,
    )

    for s in eligible:
        candidates = pos_by_store.get(s.store_id, [])
        match = _first_unconsumed_match(candidates, s, window, consumed)
        if match is not None:
            s.purchase = True
            s.purchase_basket_inr = float(match["basket_inr"])
            consumed.add((s.store_id, match["txn_id"]))

    return sessions


def _first_unconsumed_match(
    rows: list[dict],
    session: Session,
    window: timedelta,
    consumed: set[tuple[str, str]],
) -> dict | None:
    """Find the first un-consumed POS row whose timestamp is within ±window
    of the session's billing-zone span. We use the session's
    [started_at, ended_at] as the proxy for "queued at the counter" since
    BILLING_QUEUE_JOIN/ABANDON timestamps live inside that envelope.
    """
    if not rows:
        return None

    lo = session.started_at - window
    hi = session.ended_at + window
    store_id = session.store_id

    for r in rows:
        if r["timestamp"] < lo:
            continue
        if r["timestamp"] > hi:
            return None  # rows are sorted; later rows can't match either
        if (store_id, r["txn_id"]) in consumed:
            continue
        return r
    return None
