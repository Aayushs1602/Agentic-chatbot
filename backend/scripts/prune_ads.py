"""Remove sponsor reads from an already-ingested index.

    docker compose exec backend python -m scripts.prune_ads --dry-run
    docker compose exec backend python -m scripts.prune_ads

`app.rag.ingest` filters ads at ingestion time, so a fresh ingest never needs
this. It exists for two real situations: an index built before the filter
existed, and tuning the rules in `chunker.looks_like_ad` without paying a full
re-embed (~25 minutes) to see the effect.

Chunk `ord` values are left with gaps after a prune. That is deliberate — `ord`
records position in the source transcript, and renumbering would break the
correspondence between a stored chunk and the text it was cut from. Nothing
depends on `ord` being contiguous; the UNIQUE(episode_id, ord) constraint only
requires it to be distinct.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from app.db import pool as db
from app.logging import configure_logging, get_logger
from app.rag.chunker import looks_like_ad

log = get_logger("prune_ads")


async def prune(*, dry_run: bool = True, sample: int = 8) -> dict:
    rows = await db.fetch("SELECT id, text FROM chunks")
    doomed = [r for r in rows if looks_like_ad(r["text"])]

    print(f"chunks scanned : {len(rows)}")
    print(f"ads detected   : {len(doomed)} ({100 * len(doomed) / max(1, len(rows)):.1f}%)")

    if doomed:
        print(f"\nsample of what would be removed (first {sample}):")
        for row in doomed[:sample]:
            print(f"  - {' '.join(row['text'].split())[:110]}")

    if dry_run:
        print("\nDry run — nothing deleted. Re-run without --dry-run to apply.")
        return {"scanned": len(rows), "detected": len(doomed), "deleted": 0}

    if doomed:
        await db.execute(
            "DELETE FROM chunks WHERE id = ANY($1::uuid[])", [r["id"] for r in doomed]
        )
        log.info("ads_pruned", deleted=len(doomed))

    remaining = await db.fetchval("SELECT count(*) FROM chunks")
    print(f"\nDeleted {len(doomed)}. {remaining} chunks remain.")
    return {"scanned": len(rows), "detected": len(doomed), "deleted": len(doomed)}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.prune_ads")
    parser.add_argument("--dry-run", action="store_true", help="report without deleting")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        asyncio.run(prune(dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001
        print(f"Prune failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
