"""Brigade Road, Bangalore — real Purplle store layout.

Reverse-engineered from the floor plan in
`data/provided_context/Brigade Road - Store layout.xlsx`. The XLSX uses
millimetres (~13 m × 5 m); we project that onto a 1920×1080 image-coord
canvas because that's the resolution the brief assumes for CCTV (1080p).

Image-space convention used everywhere in the pipeline:
    origin = top-left, x → right, y → DOWN.

Rough mapping (XLSX mm → 1920×1080 canvas):
    store length  ~13.4 m → x ∈ [0, 1920]
    store depth    ~5.0 m → y ∈ [0, 1080]

Visitors walk in through the bottom-left **glass door** (Z_ENTRY), browse the
**south makeup wall** (Maybelline, Faces Canada, Colorbar+Sugar, Renee NY Bae,
Alps Goodness, Streax) or **north skincare wall** (EB Korean, Face Shop, Good
Vibes, DermDoc, Minimalist, Aqualogica, Lakme Skin), interact with the
**central F.O.H** (open floor + makeup demo unit + chairs), optionally visit
the specialty units (**Nail/Fragrance** on the left, **PMU booth** on the
bottom-right), and finally exit via the **CASH COUNTER** (Z_BILLING) at the
top-right before leaving through the same glass door.

This gives us a 4-stage funnel that maps cleanly onto the brief:
    Entry → Browse (aisle/FOH/makeup) → Billing → Purchase
"""

from __future__ import annotations

from app.domain.zones import StoreLayout, Zone

# ── Default store identity (Real Purplle Brigade Road, Bangalore) ───────
BRIGADE_STORE_ID = "ST1008"
BRIGADE_STORE_NAME = "Brigade_Bangalore"


def brigade_road_layout(store_id: str = BRIGADE_STORE_ID) -> StoreLayout:
    """Return the eight-zone Brigade Road layout on a 1920×1080 canvas."""
    return StoreLayout(
        store_id=store_id,
        zones=(
            # ── ENTRY (glass door, bottom-left) ────────────────────────
            Zone(
                zone_id="Z_ENTRY",
                name="Entry / Glass Door",
                # A vertical strip on the very left, full image height.
                polygon=((0.0, 0.0), (180.0, 0.0), (180.0, 1080.0), (0.0, 1080.0)),
                is_billing=False,
            ),
            # ── NORTH AISLE (skincare wall, top band) ──────────────────
            Zone(
                zone_id="Z_NORTH_AISLE",
                name="North Skincare Aisle",
                polygon=(
                    (180.0, 0.0),
                    (1700.0, 0.0),
                    (1700.0, 200.0),
                    (180.0, 200.0),
                ),
                is_billing=False,
            ),
            # ── SOUTH AISLE (makeup wall, bottom band) ─────────────────
            Zone(
                zone_id="Z_SOUTH_AISLE",
                name="South Makeup Aisle",
                polygon=(
                    (180.0, 880.0),
                    (1500.0, 880.0),
                    (1500.0, 1080.0),
                    (180.0, 1080.0),
                ),
                is_billing=False,
            ),
            # ── NAIL / FRAGRANCE specialty unit ────────────────────────
            Zone(
                zone_id="Z_NAIL_FRAG",
                name="Nail & Fragrance Unit",
                polygon=(
                    (180.0, 200.0),
                    (480.0, 200.0),
                    (480.0, 880.0),
                    (180.0, 880.0),
                ),
                is_billing=False,
            ),
            # ── MAKEUP demo island (must come BEFORE Z_FOH) ───────────
            # Z_MAKEUP physically sits *inside* the Z_FOH rectangle (the
            # demo unit + chairs are in the middle of the open floor).
            # `StoreLayout.zone_at` returns the first matching polygon, so
            # the more-specific zone (Z_MAKEUP) is listed first — anything
            # outside the makeup island but still in the central rectangle
            # falls through to Z_FOH, which is what we want.
            Zone(
                zone_id="Z_MAKEUP",
                name="Makeup Demo Unit",
                polygon=(
                    (820.0, 380.0),
                    (1180.0, 380.0),
                    (1180.0, 700.0),
                    (820.0, 700.0),
                ),
                is_billing=False,
            ),
            # ── F.O.H — central open floor (excluding makeup-unit island)
            Zone(
                zone_id="Z_FOH",
                name="Front of House",
                polygon=(
                    (480.0, 200.0),
                    (1500.0, 200.0),
                    (1500.0, 880.0),
                    (480.0, 880.0),
                ),
                is_billing=False,
            ),
            # ── BILLING / CASH COUNTER ────────────────────────────────
            Zone(
                zone_id="Z_BILLING",
                name="Cash Counter",
                polygon=(
                    (1500.0, 200.0),
                    (1900.0, 200.0),
                    (1900.0, 700.0),
                    (1500.0, 700.0),
                ),
                is_billing=True,
            ),
            # ── PMU (Permanent Makeup) booth — bottom-right ───────────
            Zone(
                zone_id="Z_PMU",
                name="Permanent Makeup Booth",
                polygon=(
                    (1500.0, 700.0),
                    (1900.0, 700.0),
                    (1900.0, 1080.0),
                    (1500.0, 1080.0),
                ),
                is_billing=False,
            ),
        ),
    )


# Shorter, ergonomic name for callers that want the default store.
def brigade_layout() -> StoreLayout:
    return brigade_road_layout()
