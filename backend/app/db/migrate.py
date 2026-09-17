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

import asyncpg

from app.config import settings
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
    applied: List[str] = []

    # A dedicated connection on the OWNER credentials, not the shared pool.
    # The pool now authenticates as the restricted application role, which
    # deliberately cannot CREATE or ALTER anything — so migrations run here or
    # they do not run at all.
    conn = await asyncpg.connect(settings.asyncpg_dsn)
    try:
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
    finally:
        await conn.close()

    if applied:
        log.info("migrations_complete", applied=applied)
    else:
        log.info("migrations_up_to_date", count=len(_migration_files()))
    return applied


async def provision_app_role() -> None:
    """Give the application role a password so the app can log in as it.

    Runs on owner credentials, like migrations, because only the owner can
    ALTER a role. 003_rls.sql creates `lenny_app` NOLOGIN; this is what turns
    it into something the app can authenticate as.

    A no-op when APP_DB_PASSWORD is unset — the password belongs in the
    environment, not in a migration file, for the same reason the dev API key
    does. Only ever logs the role name.
    """
    if not settings.app_db_password:
        return

    conn = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'lenny_app'"
        )
        if not exists:
            log.warning("app_role_missing", hint="Is 003_rls applied?")
            return
        # ALTER ROLE is DDL and cannot take a bind parameter, so the password
        # has to be inlined into the statement text. `format(..., %L, $1)` makes
        # Postgres do the literal-quoting, which is the one escaping
        # implementation guaranteed to agree with its own parser — building this
        # string in Python would be hand-rolled escaping of a credential.
        # `$1::text`, not bare `$1`: format()'s value arguments are declared
        # "any", so Postgres cannot infer a parameter type and asyncpg fails
        # with IndeterminateDatatypeError at prepare time.
        statement = await conn.fetchval(
            "SELECT format('ALTER ROLE lenny_app LOGIN PASSWORD %L', $1::text)",
            settings.app_db_password,
        )
        await conn.execute(statement)
        log.info("app_role_provisioned", role="lenny_app")
    finally:
        await conn.close()
