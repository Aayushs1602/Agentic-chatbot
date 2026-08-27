"""Minimal forward-only migration runner.

Applies `migrations/*.sql` in filename order, once each, inside a transaction,
recording every application in `schema_migrations`. Roughly 40 lines instead of
an Alembic dependency — which buys autogeneration and downgrades we do not need,
at the cost of a second source of truth for a schema that is already hand-written
SQL (pgvector operators, generated tsvector columns).

Rollback story for the handoff docs: forward-only. To roll back, restore from a
`pg_dump` taken before the migration, or add a compensating `NNN_*.sql`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.db.pool import get_pool
from app.logging import get_logger

log = get_logger("migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _migration_files() -> List[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def run_migrations() -> List[str]:
    """Apply pending migrations. Returns the versions applied this run."""
    pool = await get_pool()
    applied: List[str] = []

    async with pool.acquire() as conn:
        await conn.execute(_BOOTSTRAP)
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        for path in _migration_files():
            version = path.stem
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            log.info("migration_applying", version=version)
            # One transaction per migration: a failure leaves the database on
            # the last good version rather than half-migrated.
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            applied.append(version)
            log.info("migration_applied", version=version)

    if applied:
        log.info("migrations_complete", applied=applied)
    else:
        log.info("migrations_up_to_date", count=len(_migration_files()))
    return applied
