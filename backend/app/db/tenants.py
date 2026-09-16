"""Data access for tenants and API keys.

Separate from `repository.py`, which is scoped to one conversation's data.
This module is about *who is asking*, and it runs before that data is touched.

Note that nothing here is tenant-scoped by RLS, and that is deliberate:
authentication has to read the `api_keys` row in order to learn which tenant
the caller is, so it necessarily runs before a tenant is known. This module is
the one place that legitimately reads across tenants, which is exactly why it
is small, has no query that takes a caller-supplied filter, and stays that way.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.db import pool as db
from app.errors import NotFoundError
from app.logging import get_logger
from app.security.api_keys import GeneratedKey, generate_key, hash_key

log = get_logger("db.tenants")

# How stale `last_used_at` is allowed to get. Updating it on every request
# would put a write on the hot path of every authenticated call to answer a
# question ("is this key still in use?") that nobody asks to the second.
#
# A timedelta, not a string: asyncpg binds `$n::interval` as a real interval
# and rejects text for it. Passing "60 seconds" raised DataError at runtime
# while every unit test passed, because none of them reach Postgres.
_LAST_USED_STALENESS = timedelta(seconds=60)


# ── Tenants ─────────────────────────────────────────────────────────────


async def get_tenant(tenant_id: UUID) -> Dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, slug, name, plan, status, rpm_limit, rpd_limit, created_at "
        "FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None:
        raise NotFoundError(f"No tenant {tenant_id}.")
    return dict(row)


async def get_tenant_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    row = await db.fetchrow(
        "SELECT id, slug, name, plan, status, rpm_limit, rpd_limit, created_at "
        "FROM tenants WHERE slug = $1",
        slug,
    )
    return dict(row) if row else None


async def create_tenant(*, slug: str, name: str, plan: str = "free") -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO tenants (slug, name, plan) VALUES ($1, $2, $3)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, slug, name, plan, status, rpm_limit, rpd_limit, created_at
        """,
        slug, name, plan,
    )
    log.info("tenant_created", slug=slug, tenant_id=str(row["id"]))
    return dict(row)


# ── API keys ────────────────────────────────────────────────────────────


async def authenticate(plaintext: str) -> Optional[Dict[str, Any]]:
    """Resolve a plaintext key to its tenant, or None.

    One round trip. The `touched` CTE refreshes `last_used_at` only when it is
    already stale, so the common case is an indexed read and no write at all.

    Returns None for every failure mode — unknown, revoked, suspended tenant —
    without distinguishing them to the caller. Telling an unauthenticated
    client *which* of those applies is free reconnaissance.
    """
    row = await db.fetchrow(
        """
        WITH k AS (
            SELECT ak.id, ak.tenant_id, ak.name AS key_name, ak.key_prefix,
                   t.slug, t.name AS tenant_name, t.plan, t.status,
                   t.rpm_limit, t.rpd_limit
            FROM api_keys ak
            JOIN tenants t ON t.id = ak.tenant_id
            WHERE ak.key_hash = $1 AND ak.revoked_at IS NULL
        ),
        touched AS (
            UPDATE api_keys SET last_used_at = now()
            WHERE id IN (SELECT id FROM k)
              AND (last_used_at IS NULL
                   OR last_used_at < now() - $2::interval)
            RETURNING id
        )
        SELECT * FROM k
        """,
        hash_key(plaintext),
        _LAST_USED_STALENESS,
    )
    if row is None:
        return None
    if row["status"] != "active":
        log.warning("auth_rejected_suspended_tenant", tenant_id=str(row["tenant_id"]))
        return None
    return dict(row)


async def create_api_key(
    tenant_id: UUID, *, name: str = "default", env: str = "live"
) -> tuple[GeneratedKey, Dict[str, Any]]:
    """Mint a key for a tenant.

    Returns the generated key *and* its stored row. The plaintext exists only
    in the returned object — it is never written anywhere — so the caller is
    responsible for showing it to the user exactly once.
    """
    generated = generate_key(env)
    row = await db.fetchrow(
        """
        INSERT INTO api_keys (tenant_id, key_prefix, key_hash, name)
        VALUES ($1, $2, $3, $4)
        RETURNING id, tenant_id, key_prefix, name, created_at, last_used_at, revoked_at
        """,
        tenant_id, generated.prefix, generated.hash, name,
    )
    log.info("api_key_created", tenant_id=str(tenant_id), key_prefix=generated.prefix)
    return generated, dict(row)


async def upsert_api_key(
    tenant_id: UUID, plaintext: str, *, name: str = "default"
) -> Dict[str, Any]:
    """Register a caller-supplied key. Used only to seed a local dev key.

    Separate from `create_api_key` because it takes a plaintext the caller
    already has, which is a thing you want to be able to grep for.
    """
    from app.security.api_keys import key_prefix

    row = await db.fetchrow(
        """
        INSERT INTO api_keys (tenant_id, key_prefix, key_hash, name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (key_hash) DO UPDATE SET revoked_at = NULL
        RETURNING id, tenant_id, key_prefix, name, created_at, last_used_at, revoked_at
        """,
        tenant_id, key_prefix(plaintext), hash_key(plaintext), name,
    )
    return dict(row)


async def bootstrap_dev_key() -> None:
    """Register `DEV_API_KEY` against the `dev` tenant, if one is configured.

    Idempotent, and a no-op when the variable is unset. This is the seam that
    keeps a working local key out of the repository: the migration creates the
    tenant, the environment supplies the secret, and the two only meet on a
    developer's machine.

    Only ever logs the prefix. The plaintext exists in memory and in the
    operator's `.env`, and nowhere else.
    """
    from app.config import settings
    from app.security.api_keys import key_prefix

    if not settings.dev_api_key:
        return

    dev = await get_tenant_by_slug("dev")
    if dev is None:
        log.warning("dev_key_skipped", reason="no dev tenant — is 002_tenancy applied?")
        return

    await upsert_api_key(dev["id"], settings.dev_api_key, name="local-dev")
    log.info(
        "dev_key_ready",
        tenant="dev",
        key_prefix=key_prefix(settings.dev_api_key),
    )


async def list_api_keys(tenant_id: UUID) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT id, key_prefix, name, created_at, last_used_at, revoked_at
        FROM api_keys WHERE tenant_id = $1 ORDER BY created_at DESC
        """,
        tenant_id,
    )
    return [dict(r) for r in rows]


async def revoke_api_key(tenant_id: UUID, key_id: UUID) -> None:
    """Revoke a key.

    Scoped by tenant_id as well as key id: without it, knowing any key's UUID
    would be enough to revoke it from another tenant's account.
    """
    result = await db.execute(
        "UPDATE api_keys SET revoked_at = now() "
        "WHERE id = $1 AND tenant_id = $2 AND revoked_at IS NULL",
        key_id, tenant_id,
    )
    if result.endswith(" 0"):
        raise NotFoundError(f"No active key {key_id}.")
    log.info("api_key_revoked", tenant_id=str(tenant_id), key_id=str(key_id))
