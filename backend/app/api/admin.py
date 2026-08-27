"""Corpus inspection.

Read-only views over what ingestion actually produced. This exists because
several real defects in this project were invisible in every metric and obvious
the moment someone looked at the chunks: passages beginning mid-word, sponsor
reads scoring 0.710 as top sources, transcript timestamp markers being read as
episode durations.

So the endpoints do not just page through rows — they flag the specific problems
that have bitten before, and the dashboard sorts by them. A corpus you cannot
look at is a corpus you cannot trust.

Read-only by design: no auth exists in this product (see the PRD), so nothing
here may mutate anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Query

from app.db import pool as db
from app.errors import NotFoundError
from app.rag.chunker import looks_like_ad

router = APIRouter(tags=["admin"])


def _flags(text: str, start_seconds: Optional[int], token_count: int) -> List[str]:
    """Quality problems worth a human's attention.

    Each of these corresponds to a bug that actually shipped at some point.
    """
    flags: List[str] = []
    if looks_like_ad(text):
        flags.append("ad")
    # A chunk that opens mid-word means the chunker advanced without snapping
    # to a boundary — it corrupts both the embedding and the quoted citation.
    if text[:1].islower() and not text[:1].isdigit():
        flags.append("starts-midword")
    if start_seconds is None:
        flags.append("no-timestamp")
    if token_count < 50:
        flags.append("very-short")
    return flags


@router.get("/admin/stats", summary="Corpus health at a glance")
async def stats() -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM episodes) AS episodes,
          (SELECT count(*) FROM chunks) AS chunks,
          (SELECT count(*) FROM episodes WHERE published_on IS NULL) AS undated,
          (SELECT count(*) FROM episodes WHERE duration_s IS NULL OR duration_s = 0) AS untimed,
          (SELECT count(*) FROM chunks WHERE start_seconds IS NULL) AS chunks_no_timestamp,
          (SELECT round(avg(token_count)) FROM chunks) AS avg_tokens,
          (SELECT min(token_count) FROM chunks) AS min_tokens,
          (SELECT max(token_count) FROM chunks) AS max_tokens
        """
    )
    last = await db.fetchrow(
        "SELECT started_at, finished_at, episodes_ingested, chunks_written, status "
        "FROM ingest_runs ORDER BY started_at DESC LIMIT 1"
    )
    return {
        "corpus": {
            "episodes": row["episodes"],
            "chunks": row["chunks"],
            "avg_tokens": int(row["avg_tokens"] or 0),
            "min_tokens": row["min_tokens"],
            "max_tokens": row["max_tokens"],
        },
        "gaps": {
            "episodes_without_date": row["undated"],
            "episodes_without_duration": row["untimed"],
            "chunks_without_timestamp": row["chunks_no_timestamp"],
        },
        "last_ingest": dict(last) if last else None,
    }


@router.get("/admin/episodes", summary="Browse ingested episodes")
async def list_episodes(
    q: Optional[str] = Query(None, description="Filter by title or guest"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order: str = Query("title", pattern="^(title|duration|date|chunks)$"),
) -> Dict[str, Any]:
    order_sql = {
        "title": "e.title ASC",
        "duration": "e.duration_s DESC NULLS LAST",
        "date": "e.published_on DESC NULLS LAST",
        "chunks": "chunk_count DESC",
    }[order]

    rows = await db.fetch(
        f"""
        SELECT e.id, e.slug, e.title, e.guests, e.published_on, e.duration_s,
               e.youtube_url, e.ingested_at,
               (SELECT count(*) FROM chunks c WHERE c.episode_id = e.id) AS chunk_count
        FROM episodes e
        WHERE $1::text IS NULL
           OR e.title ILIKE '%' || $1 || '%'
           OR EXISTS (SELECT 1 FROM unnest(e.guests) g WHERE g ILIKE '%' || $1 || '%')
        ORDER BY {order_sql}
        LIMIT $2 OFFSET $3
        """,
        q, limit, offset,
    )
    total = await db.fetchval(
        """
        SELECT count(*) FROM episodes e
        WHERE $1::text IS NULL
           OR e.title ILIKE '%' || $1 || '%'
           OR EXISTS (SELECT 1 FROM unnest(e.guests) g WHERE g ILIKE '%' || $1 || '%')
        """,
        q,
    )
    return {
        "total": total,
        "offset": offset,
        "episodes": [
            {
                "id": str(r["id"]),
                "slug": r["slug"],
                "title": r["title"],
                "guests": list(r["guests"] or []),
                "published_on": r["published_on"].isoformat() if r["published_on"] else None,
                "duration_s": r["duration_s"],
                "youtube_url": r["youtube_url"],
                "chunk_count": r["chunk_count"],
                "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/admin/episodes/{episode_id}/chunks", summary="Chunks for one episode")
async def episode_chunks(episode_id: UUID) -> Dict[str, Any]:
    episode = await db.fetchrow(
        "SELECT id, slug, title, guests, published_on, duration_s, youtube_url, "
        "source_path, content_sha256 FROM episodes WHERE id = $1",
        episode_id,
    )
    if episode is None:
        raise NotFoundError(f"No episode {episode_id}.")

    rows = await db.fetch(
        "SELECT id, ord, text, token_count, start_char, end_char, start_seconds "
        "FROM chunks WHERE episode_id = $1 ORDER BY ord",
        episode_id,
    )

    chunks = []
    for r in rows:
        flags = _flags(r["text"], r["start_seconds"], r["token_count"])
        chunks.append(
            {
                "id": str(r["id"]),
                "ord": r["ord"],
                "text": r["text"],
                "token_count": r["token_count"],
                "start_char": r["start_char"],
                "end_char": r["end_char"],
                "start_seconds": r["start_seconds"],
                "flags": flags,
            }
        )

    return {
        "episode": {
            "id": str(episode["id"]),
            "slug": episode["slug"],
            "title": episode["title"],
            "guests": list(episode["guests"] or []),
            "published_on": episode["published_on"].isoformat() if episode["published_on"] else None,
            "duration_s": episode["duration_s"],
            "youtube_url": episode["youtube_url"],
            "source_path": episode["source_path"],
            "content_sha256": episode["content_sha256"][:12],
        },
        "chunk_count": len(chunks),
        "flagged": sum(1 for c in chunks if c["flags"]),
        "chunks": chunks,
    }


@router.get("/admin/flagged", summary="Chunks worth a human's attention")
async def flagged(limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
    """Scan for the specific defects that have shipped before.

    Scans in Python rather than SQL because `looks_like_ad` is the same function
    ingestion uses — one definition, so the dashboard can never disagree with
    the filter about what an advert is.
    """
    rows = await db.fetch(
        """
        SELECT c.id, c.ord, c.text, c.token_count, c.start_seconds,
               e.id AS episode_id, e.title
        FROM chunks c JOIN episodes e ON e.id = c.episode_id
        ORDER BY e.title, c.ord
        """
    )

    found: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for r in rows:
        flags = _flags(r["text"], r["start_seconds"], r["token_count"])
        # "no-timestamp" alone is common and low-signal; it is counted but does
        # not by itself put a chunk in front of a reviewer.
        actionable = [f for f in flags if f != "no-timestamp"]
        for f in flags:
            counts[f] = counts.get(f, 0) + 1
        if actionable and len(found) < limit:
            found.append(
                {
                    "id": str(r["id"]),
                    "episode_id": str(r["episode_id"]),
                    "title": r["title"],
                    "ord": r["ord"],
                    "text": r["text"][:400],
                    "token_count": r["token_count"],
                    "flags": flags,
                }
            )

    return {"scanned": len(rows), "counts": counts, "chunks": found}
