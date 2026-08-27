"""asyncpg connection pool.

Deliberately lazy and forgiving: the pool is created on first use and a failure
to connect raises `DatabaseUnavailableError` rather than crashing the process.
The app must still start when Postgres is down — otherwise `/readyz` can't tell
anyone *that* Postgres is down, which is exactly when you need it most.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg

from app.config import settings
from app.errors import DatabaseUnavailableError
from app.logging import get_logger

log = get_logger("db")

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup: jsonb codecs, and the HNSW search width."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    # pgvector's hnsw.ef_search defaults to 40 and its guidance is that it
    # should be at least the query's LIMIT — we ask for RETRIEVAL_CANDIDATES
    # (80) per retriever, so the default is below the recommendation.
    #
    # Measured at the current corpus size (18.8k vectors), recall@80 against a
    # brute-force ground truth is 100% even at the default, so this changes
    # nothing today. It is set anyway because the guidance is explicit, the
    # cost is zero, and the margin narrows as the corpus grows — a silent
    # recall drop is exactly the kind of failure this system has already been
    # bitten by twice.
    #
    # The GUC only exists once pgvector's library is loaded into the session,
    # so a fresh connection can legitimately not have it yet.
    try:
        await conn.execute(
            f"SET hnsw.ef_search = {max(64, settings.retrieval_candidates * 2)}"
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("hnsw_ef_search_not_set", error=str(exc))


async def create_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.asyncpg_dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            init=_init_connection,
            command_timeout=60,
            # Bounded so /readyz stays fast when the DB host doesn't resolve —
            # an unreachable database should be *reported* in a second, not
            # discovered after the default connect timeout.
            timeout=5.0,
        )
        log.info("db_pool_created", min=settings.db_pool_min, max=settings.db_pool_max)
        return _pool
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured 503
        log.error("db_pool_failed", error=str(exc))
        raise DatabaseUnavailableError(
            "Could not connect to Postgres.",
            detail={
                "hint": "Is the `db` service running? Try `docker compose ps`.",
                "error": str(exc),
            },
        ) from exc


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        return await create_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


async def ping() -> bool:
    """Cheap liveness probe used by /readyz. Never raises."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("db_ping_failed", error=str(exc))
        return False


async def fetch(query: str, *args: Any) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
