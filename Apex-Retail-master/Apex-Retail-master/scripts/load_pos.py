"""CLI: load POS transactions into the DB.

Auto-detects between two formats:
    * **Purplle real export** (38-column, `order_id` etc. — see provided_context).
      Recognised by the presence of `order_id` and `total_amount` headers.
    * **Simplified canonical** (`store_id`, `txn_id`, `timestamp`, `basket_inr`)
      — the format spelled out in the challenge brief, used by tests.

Usage:
    python -m scripts.load_pos                   # auto-detect, default ./data/pos_transactions.csv
    python -m scripts.load_pos --path file.csv

Idempotent — re-running on the same file inserts 0 new rows.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from app.core.logging import configure_logging, get_logger
from app.infra.db import session_scope
from app.infra.loaders.pos_loader import POSLoadError, load_pos_csv
from app.infra.loaders.purplle_pos_loader import PurplePOSLoadError, load_purplle_pos
from app.infra.repositories.pos_repo import POSRepository

DEFAULT_PATH = Path("data") / "pos_transactions.csv"


def _looks_like_purplle(path: Path) -> bool:
    """Sniff the header — Purplle exports always carry `order_id` AND `total_amount`."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
    except OSError:
        return False
    cols = {c.strip() for c in header}
    return "order_id" in cols and "total_amount" in cols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load POS transactions into the DB.")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--format",
        choices=("auto", "purplle", "canonical"),
        default="auto",
        help="Force a parser instead of header sniffing.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    log = get_logger(__name__)

    if not args.path.exists():
        log.error("pos.file_missing", path=str(args.path))
        print(f"❌ File not found: {args.path}", file=sys.stderr)
        return 2

    fmt = args.format
    if fmt == "auto":
        fmt = "purplle" if _looks_like_purplle(args.path) else "canonical"

    try:
        if fmt == "purplle":
            txns = load_purplle_pos(args.path)
            log.info("pos.parsed", format="purplle", rows=len(txns))
        else:
            txns = load_pos_csv(args.path)
            log.info("pos.parsed", format="canonical", rows=len(txns))
    except (POSLoadError, PurplePOSLoadError) as exc:
        log.error("pos.parse_failed", error=str(exc))
        print(f"❌ Parse error: {exc}", file=sys.stderr)
        return 1

    with session_scope() as sess:
        repo = POSRepository(sess)
        inserted, skipped = repo.upsert_many(txns)

    log.info("pos.loaded", format=fmt, inserted=inserted, skipped=skipped)
    print(
        f"✅ Loaded {inserted} new POS transactions from {args.path.name} "
        f"({skipped} duplicates skipped, format={fmt})."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — entrypoint
    raise SystemExit(main())
