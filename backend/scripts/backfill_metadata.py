"""Refresh episode metadata without re-embedding.

    docker compose exec backend python -m scripts.backfill_metadata

Re-parses each transcript's frontmatter and updates the `episodes` row only.
Chunks and embeddings are untouched, so this takes seconds rather than the ~25
minutes a full re-ingest costs.

It exists because a frontmatter key was misnamed in the parser: this corpus
uses `publish_date`, which was not in the list of keys tried, so all 303
`published_on` values were NULL. Ingestion reported success throughout — the
column was simply never populated, and every date-ordered catalogue query
silently had nothing to sort by.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.db import pool as db
from app.logging import configure_logging, get_logger
from app.rag.ingest import discover_transcripts, ensure_corpus, parse_episode

log = get_logger("backfill")

_UPDATE = """
UPDATE episodes SET
    title = $2, guests = $3, youtube_url = $4, video_id = $5,
    published_on = $6::date, duration_s = $7, description = $8
WHERE slug = $1
"""


async def backfill() -> int:
    root = ensure_corpus()
    paths = discover_transcripts(root)
    updated = skipped = 0

    for path in paths:
        try:
            episode, _ = parse_episode(path, root)
        except Exception as exc:  # noqa: BLE001
            log.warning("parse_failed", path=str(path), error=str(exc))
            skipped += 1
            continue
        await db.execute(
            _UPDATE,
            episode["slug"], episode["title"], episode["guests"],
            episode["youtube_url"], episode["video_id"], episode["published_on"],
            episode["duration_s"], episode["description"],
        )
        updated += 1

    row = await db.fetchrow(
        "SELECT count(*) AS total, count(published_on) AS dated, "
        "count(duration_s) AS timed FROM episodes"
    )
    print(f"updated {updated} episodes ({skipped} skipped)")
    print(f"now: {row['dated']}/{row['total']} dated, {row['timed']}/{row['total']} with duration")
    return 0


def main() -> int:
    configure_logging()
    try:
        return asyncio.run(backfill())
    except Exception as exc:  # noqa: BLE001
        print(f"Backfill failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
