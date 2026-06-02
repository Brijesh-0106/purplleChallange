"""Funnel service.

Builds the rubric's required 4-stage funnel:

    Entry  →  Browse  →  Billing  →  Purchase

Where:
    * **Entry**     — every session counts (one per unique visitor).
    * **Browse**    — sessions that visited at least one non-billing zone
                       (Z_NORTH_AISLE, Z_SOUTH_AISLE, Z_FOH, Z_MAKEUP, etc.).
    * **Billing**   — sessions whose person reached the billing queue
                       (`billing_join_count >= 1`).
    * **Purchase**  — sessions where POS correlation matched a transaction
                       (`session.purchase`).

Session-based counting → no double-counting. A customer with 3 ENTRYs and
2 REENTRYs counts as 1 session, hence 1 in each stage they reached.

Staff sessions are excluded — visitor-side metrics shouldn't include the
people working the floor.

Drop-off between stages is a derived field for direct-display in the
dashboard / response.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.sessions import Session


@dataclass(slots=True)
class FunnelStage:
    """One stage of the funnel."""

    name: str                  # "entry" | "browse" | "billing" | "purchase"
    sessions: int              # how many UNIQUE visitor sessions reached this stage
    drop_off_from_previous: float = 0.0  # 0..1; visitors who reached prev but not this


@dataclass(slots=True)
class FunnelReport:
    store_id: str
    stages: list[FunnelStage]

    @property
    def overall_conversion(self) -> float:
        """Purchases ÷ Entries — same number as `metrics.conversion_rate`."""
        if not self.stages:
            return 0.0
        first = next((s for s in self.stages if s.name == "entry"), None)
        last = next((s for s in self.stages if s.name == "purchase"), None)
        if first is None or last is None or first.sessions == 0:
            return 0.0
        return round(last.sessions / first.sessions, 4)


def compute_funnel(sessions: list[Session], *, store_id: str) -> FunnelReport:
    customers = [s for s in sessions if not s.is_staff]
    n_entry = len(customers)
    # Browse = visited any non-billing zone. We approximate with "zones_visited
    # contains anything other than the billing zone" — the EventBuilder
    # adds the billing zone to `zones_visited` only on JOIN, so a session
    # that ONLY billed (no browse) won't pass this gate.
    n_browse = sum(
        1
        for s in customers
        if any(not z.endswith("BILLING") for z in s.zones_visited)
        # Endswith is a lightweight heuristic; the canonical billing zone
        # is `Z_BILLING` (Brigade layout). If a future store uses a
        # differently-named billing zone, replace this with a layout lookup.
    )
    n_billing = sum(1 for s in customers if s.reached_billing)
    n_purchase = sum(1 for s in customers if s.purchase)

    counts = [
        ("entry", n_entry),
        ("browse", n_browse),
        ("billing", n_billing),
        ("purchase", n_purchase),
    ]
    stages: list[FunnelStage] = []
    prev = None
    for name, count in counts:
        stage = FunnelStage(name=name, sessions=count)
        if prev is not None and prev.sessions > 0:
            stage.drop_off_from_previous = round(
                max(prev.sessions - count, 0) / prev.sessions, 4
            )
        stages.append(stage)
        prev = stage

    return FunnelReport(store_id=store_id, stages=stages)
