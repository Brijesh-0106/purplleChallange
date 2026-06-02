"""CLI: load `data/store_layout.json` into the DB (one row per store)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.logging import configure_logging, get_logger
from app.infra.db import session_scope
from app.infra.loaders.layout_loader import LayoutLoadError, load_raw, load_store_layouts
from app.infra.repositories.layout_repo import StoreLayoutRepository

DEFAULT_PATH = Path("data") / "store_layout.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load store layouts into the DB.")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    configure_logging()
    log = get_logger(__name__)

    if not args.path.exists():
        log.error("layout.file_missing", path=str(args.path))
        print(f"❌ File not found: {args.path}", file=sys.stderr)
        return 2

    try:
        # Validate first (raises on bad polygons), then store the raw JSON.
        layouts = load_store_layouts(args.path)
        raw = load_raw(args.path)
    except LayoutLoadError as exc:
        log.error("layout.parse_failed", error=str(exc))
        print(f"❌ Parse error: {exc}", file=sys.stderr)
        return 1

    log.info("layout.parsed", stores=len(layouts))

    with session_scope() as sess:
        repo = StoreLayoutRepository(sess)
        for store_id in layouts:
            repo.upsert(store_id, raw[store_id])

    print(f"✅ Loaded layouts for {len(layouts)} stores: {sorted(layouts)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
