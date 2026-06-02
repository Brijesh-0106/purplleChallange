"""Store-layout JSON loader.

Expected schema (one file per dataset, keyed by store_id):

{
  "STORE_BLR_002": {
    "zones": [
      {"zone_id": "Z_ENTRY", "name": "Entry", "polygon": [[x, y], ...], "is_billing": false},
      {"zone_id": "Z_BILLING", "name": "Billing", "polygon": [[x, y], ...], "is_billing": true}
    ]
  },
  ...
}
"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.zones import StoreLayout, Zone


class LayoutLoadError(ValueError):
    """Bad / unparseable layout file."""


def load_store_layouts(path: Path) -> dict[str, StoreLayout]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LayoutLoadError("store_layout.json must be an object keyed by store_id")

    out: dict[str, StoreLayout] = {}
    for store_id, body in raw.items():
        if not isinstance(body, dict) or "zones" not in body:
            raise LayoutLoadError(f"store {store_id}: missing 'zones'")
        out[store_id] = _parse_store(store_id, body["zones"])
    return out


def load_raw(path: Path) -> dict:
    """Return the JSON tree verbatim — used by the layout repo to cache it."""
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_store(store_id: str, zones_raw: list) -> StoreLayout:
    zones: list[Zone] = []
    for z in zones_raw:
        try:
            polygon = tuple((float(x), float(y)) for x, y in z["polygon"])
        except (KeyError, ValueError, TypeError) as exc:
            raise LayoutLoadError(f"store {store_id}: bad polygon ({exc})") from exc

        if len(polygon) < 3:
            raise LayoutLoadError(
                f"store {store_id}: zone '{z.get('zone_id', '?')}' polygon needs ≥3 vertices"
            )

        zones.append(
            Zone(
                zone_id=str(z["zone_id"]),
                name=str(z.get("name", z["zone_id"])),
                polygon=polygon,
                is_billing=bool(z.get("is_billing", False)),
            )
        )
    return StoreLayout(store_id=store_id, zones=tuple(zones))
