"""Compatibility shim — kept for any caller that still imports `demo_layout`.

The real default is now the Brigade Road, Bangalore Purplle store
(`pipeline.layouts.brigade_road.brigade_road_layout`). Reverse-engineered
from `data/provided_context/Brigade Road - Store layout.xlsx`.
"""

from __future__ import annotations

from app.domain.zones import StoreLayout
from pipeline.layouts.brigade_road import brigade_road_layout


def demo_layout(store_id: str = "ST1008") -> StoreLayout:
    """Return the Brigade Road, Bangalore layout.

    The argument name and default historically pointed at a synthetic
    `STORE_DEMO_001` placeholder; we keep the function signature for
    backward compatibility but use the real store id by default.
    """
    return brigade_road_layout(store_id=store_id)
