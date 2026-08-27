"""Transcript ingestion.

    python -m app.rag.ingest                 # full corpus (269 episodes)
    python -m app.rag.ingest --limit 20      # fast subset for a first run
    python -m app.rag.ingest --force         # re-chunk and re-embed everything
    python -m app.rag.ingest --refresh       # git pull first, then ingest new work

Design notes for whoever operates this next:

* **Idempotent.** Each episode's raw file is hashed; an unchanged hash means the
  episode is skipped entirely. Re-running after an interruption resumes rather
  than duplicating, and this is also the corpus-refresh mechanism — pull the
  repo, re-run, and only changed or new episodes are processed.
* **Per-episode transactions.** Chunks for an episode are replaced atomically,
  so an interrupt can never leave an episode half-indexed.
* **Batched embedding.** The embedder is the bottleneck; batching is what keeps
  the full corpus at roughly 6-10 minutes on CPU instead of an hour.
* **Progress is recorded in `ingest_runs`**, which `/api/ingest/status` exposes,
  so a long ingest is observable without tailing logs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import frontmatter

from app.config import settings
from app.db import pool as db
from app.db.migrate import run_migrations
from app.logging import configure_logging, get_logger
from app.rag.chunker import chunk_transcript, looks_like_ad
from app.rag.embeddings import get_embedder

log = get_logger("ingest")


# ── Corpus acquisition ──────────────────────────────────────────────────


def ensure_corpus(*, refresh: bool = False) -> Path:
    """Shallow-clone the transcripts repo, or pull if it is already present."""
    target = Path(settings.transcripts_dir)
    if target.exists() and (target / "episodes").exists():
        if refresh:
            log.info("corpus_refreshing", path=str(target))
            _run(["git", "-C", str(target), "pull", "--ff-only"])
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("corpus_cloning", repo=settings.transcripts_repo, path=str(target))
    # --depth 1: we need the current transcripts, not 3 years of history.
    _run(["git", "clone", "--depth", "1", settings.transcripts_repo, str(target)])
    return target


def _run(cmd: List[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed ({result.returncode}): {result.stderr.strip()[:500]}"
        )


# ── Parsing ─────────────────────────────────────────────────────────────


def discover_transcripts(root: Path) -> List[Path]:
    """Every `episodes/<guest>/transcript.md`, in stable alphabetical order.

    Stable ordering matters: `--limit 20` must select the same 20 episodes on
    every machine, or the evaluator's results won't match the documented ones.
    """
    episodes_dir = root / "episodes"
    if not episodes_dir.exists():
        raise FileNotFoundError(
            f"No `episodes/` directory under {root}. "
            "Has the transcripts repository layout changed?"
        )
    return sorted(episodes_dir.glob("*/transcript.md"))


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _as_date(value: Any) -> Optional[date]:
    """Coerce a frontmatter date to a `datetime.date`, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _first(meta: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Frontmatter key names vary across the corpus; take the first present."""
    for key in keys:
        if meta.get(key) not in (None, "", []):
            return meta[key]
    return None


def parse_episode(path: Path, repo_root: Path) -> Tuple[Dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    post = frontmatter.loads(raw)
    meta = post.metadata or {}

    # `publish_date` is what this corpus actually uses. Omitting it left all
    # 303 published_on values NULL, which silently disabled every
    # date-ordered catalogue query while ingestion reported success.
    published = _first(
        meta, "publish_date", "published_at", "date", "published", "upload_date"
    )
    # asyncpg binds a `date` column from a real date object, not a string — a
    # `::date` cast in SQL does not rescue a str parameter. YAML gives us either
    # already, depending on quoting, so normalise here rather than at each of
    # the two call sites.
    published = _as_date(published)

    video_id = _first(meta, "video_id", "youtube_id", "videoId")
    youtube_url = _first(meta, "youtube_url", "url", "link")
    if not youtube_url and video_id:
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"

    duration = _first(meta, "duration_seconds", "duration_s", "duration")
    try:
        # Arrives as a float-ish string ("3946.0"), so via float() not int().
        duration = int(float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration = None  # some entries carry "1:23:45" rather than seconds

    episode = {
        "slug": path.parent.name,
        "title": str(_first(meta, "title", "episode_title") or path.parent.name),
        "guests": _as_list(_first(meta, "guest", "guests", "speaker", "speakers")),
        "youtube_url": youtube_url,
        "video_id": str(video_id) if video_id else None,
        "published_on": published,
        "duration_s": duration,
        "description": _first(meta, "description", "summary"),
        "source_path": str(path.relative_to(repo_root)).replace("\\", "/"),
        # Hash the raw file, frontmatter included: a metadata-only edit should
        # still re-ingest, because citations render that metadata.
        "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    return episode, post.content


# ── Persistence ─────────────────────────────────────────────────────────

_UPSERT_EPISODE = """
INSERT INTO episodes (slug, title, guests, youtube_url, video_id, published_on,
                      duration_s, description, source_path, content_sha256, ingested_at)
VALUES ($1, $2, $3, $4, $5, $6::date, $7, $8, $9, $10, now())
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    guests = EXCLUDED.guests,
    youtube_url = EXCLUDED.youtube_url,
    video_id = EXCLUDED.video_id,
    published_on = EXCLUDED.published_on,
    duration_s = EXCLUDED.duration_s,
    description = EXCLUDED.description,
    source_path = EXCLUDED.source_path,
    content_sha256 = EXCLUDED.content_sha256,
    ingested_at = now()
RETURNING id
"""


async def _existing_hashes() -> Dict[str, str]:
    rows = await db.fetch("SELECT slug, content_sha256 FROM episodes")
    return {r["slug"]: r["content_sha256"] for r in rows}


async def _store_episode(episode: Dict[str, Any], chunks: List[Dict[str, Any]]) -> int:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            episode_id = await conn.fetchval(
                _UPSERT_EPISODE,
                episode["slug"],
                episode["title"],
                episode["guests"],
                episode["youtube_url"],
                episode["video_id"],
                episode["published_on"],
                episode["duration_s"],
                episode["description"],
                episode["source_path"],
                episode["content_sha256"],
            )
            # Replace rather than upsert: a re-chunk can produce a different
            # number of chunks, and orphaned tail chunks would poison retrieval.
            await conn.execute("DELETE FROM chunks WHERE episode_id = $1", episode_id)
            await conn.executemany(
                """
                INSERT INTO chunks (episode_id, ord, text, token_count,
                                    start_char, end_char, start_seconds, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
                """,
                [
                    (
                        episode_id,
                        c["ord"],
                        c["text"],
                        c["token_count"],
                        c["start_char"],
                        c["end_char"],
                        c["start_seconds"],
                        c["embedding"],
                    )
                    for c in chunks
                ],
            )
    return len(chunks)


# ── Orchestration ───────────────────────────────────────────────────────


async def ingest(
    *,
    limit: Optional[int] = None,
    force: bool = False,
    refresh: bool = False,
) -> Dict[str, Any]:
    started = time.perf_counter()
    await run_migrations()

    root = ensure_corpus(refresh=refresh)
    paths = discover_transcripts(root)
    if limit:
        paths = paths[:limit]

    run_id = await db.fetchval(
        "INSERT INTO ingest_runs (episodes_seen) VALUES ($1) RETURNING id", len(paths)
    )
    log.info("ingest_started", run_id=str(run_id), episodes=len(paths), force=force)

    known = {} if force else await _existing_hashes()
    embedder = get_embedder()

    ingested = skipped = total_chunks = 0
    try:
        for index, path in enumerate(paths, start=1):
            try:
                episode, body = parse_episode(path, root)
            except Exception as exc:  # noqa: BLE001
                # One malformed transcript must not abort a 269-episode run.
                log.warning("episode_parse_failed", path=str(path), error=str(exc))
                continue

            if known.get(episode["slug"]) == episode["content_sha256"]:
                skipped += 1
                continue

            pieces = chunk_transcript(
                body,
                target_tokens=settings.chunk_tokens,
                overlap_tokens=settings.chunk_overlap,
            )
            # Drop sponsor reads before embedding. They are dense marketing copy
            # that retrieves well for business vocabulary and crowds out real
            # answers; see chunker.looks_like_ad for why the test is strict.
            kept = [p for p in pieces if not looks_like_ad(p.text)]
            dropped = len(pieces) - len(kept)
            if dropped:
                log.info("ad_chunks_dropped", slug=episode["slug"], dropped=dropped)
            # Re-number so `ord` stays contiguous and the UNIQUE(episode_id, ord)
            # constraint holds after removals.
            pieces = [replace(p, ord=i) for i, p in enumerate(kept)]
            if not pieces:
                log.warning("episode_empty", slug=episode["slug"])
                continue

            vectors = await embedder.embed_documents([p.text for p in pieces])
            rows = [
                {
                    "ord": p.ord,
                    "text": p.text,
                    "token_count": p.token_count,
                    "start_char": p.start_char,
                    "end_char": p.end_char,
                    "start_seconds": p.start_seconds,
                    # asyncpg has no pgvector codec; pass the text literal and cast.
                    "embedding": "[" + ",".join(f"{v:.6f}" for v in vec) + "]",
                }
                for p, vec in zip(pieces, vectors)
            ]

            written = await _store_episode(episode, rows)
            ingested += 1
            total_chunks += written
            log.info(
                "episode_ingested",
                progress=f"{index}/{len(paths)}",
                slug=episode["slug"],
                chunks=written,
            )

        elapsed = round(time.perf_counter() - started, 1)
        await db.execute(
            """
            UPDATE ingest_runs
               SET finished_at = now(), episodes_ingested = $2, episodes_skipped = $3,
                   chunks_written = $4, status = 'ok'
             WHERE id = $1
            """,
            run_id, ingested, skipped, total_chunks,
        )
        summary = {
            "run_id": str(run_id),
            "episodes_seen": len(paths),
            "episodes_ingested": ingested,
            "episodes_skipped": skipped,
            "chunks_written": total_chunks,
            "elapsed_s": elapsed,
        }
        log.info("ingest_complete", **summary)
        return summary

    except Exception as exc:  # noqa: BLE001
        await db.execute(
            "UPDATE ingest_runs SET finished_at = now(), status = 'failed', error = $2 "
            "WHERE id = $1",
            run_id, str(exc)[:2000],
        )
        log.error("ingest_failed", error=str(exc))
        raise


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.rag.ingest",
        description="Ingest Lenny's Podcast transcripts into Postgres + pgvector.",
    )
    parser.add_argument("--limit", type=int, help="only the first N episodes (stable order)")
    parser.add_argument("--force", action="store_true", help="re-ingest even if unchanged")
    parser.add_argument("--refresh", action="store_true", help="git pull the corpus first")
    args = parser.parse_args(list(argv) if argv is not None else None)

    configure_logging()
    try:
        summary = asyncio.run(
            ingest(limit=args.limit, force=args.force, refresh=args.refresh)
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume — completed episodes are skipped.")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Ingest failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nIngested {summary['episodes_ingested']} episodes "
        f"({summary['episodes_skipped']} unchanged, {summary['chunks_written']} chunks) "
        f"in {summary['elapsed_s']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
