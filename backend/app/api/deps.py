"""Request dependencies: who is calling.

`require_tenant` is the single place a request acquires an identity. Everything
downstream — the rate limiter's bucket, the usage ledger's row, and (next) the
`SET LOCAL app.tenant_id` that RLS reads — takes the tenant from here, so there
is exactly one answer to "who is this?" per request.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import Request

from app.config import settings
from app.db import tenants as tenant_repo
from app.errors import UnauthorizedError
from app.logging import get_logger, set_tenant_id
from app.security.api_keys import looks_like_key, parse_bearer

log = get_logger("api.deps")

# The tenant every pre-tenancy row was backfilled to, and the identity used
# when AUTH_REQUIRED is off. Fixed in 002_tenancy.sql.
DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
DEV_TENANT_ID = UUID("00000000-0000-0000-0000-000000000002")

_UNAUTHORIZED_HINT = {
    "hint": "Send `Authorization: Bearer sk_live_...`.",
    "docs": "Create a key with POST /api/admin/keys.",
}


async def require_tenant(request: Request) -> Dict[str, Any]:
    """Resolve the caller to a tenant, or reject the request.

    Every rejection returns the same message. Distinguishing "no such key" from
    "revoked key" from "suspended tenant" tells an unauthenticated caller
    things they have not earned the right to know.
    """
    raw = parse_bearer(request.headers.get("authorization"))

    if raw is None:
        if not settings.auth_required:
            return await _anonymous_principal()
        raise UnauthorizedError(detail=dict(_UNAUTHORIZED_HINT))

    # Shape check first, so a malformed header costs a string comparison
    # rather than a database round trip.
    if not looks_like_key(raw):
        raise UnauthorizedError(detail=dict(_UNAUTHORIZED_HINT))

    principal = await tenant_repo.authenticate(raw)
    if principal is None:
        # Logged with the prefix only. The full key must never reach the logs;
        # that is how a credential ends up in a log aggregator forever.
        log.warning("auth_failed", key_prefix=raw[:12])
        raise UnauthorizedError(detail=dict(_UNAUTHORIZED_HINT))

    set_tenant_id(str(principal["tenant_id"]))
    return principal


async def optional_tenant(request: Request) -> Optional[Dict[str, Any]]:
    """Like `require_tenant`, but tolerates an anonymous caller.

    For endpoints that are useful without an identity and better with one.
    """
    try:
        return await require_tenant(request)
    except UnauthorizedError:
        return None


async def _anonymous_principal() -> Dict[str, Any]:
    """The identity used when AUTH_REQUIRED is off.

    Returns the default tenant without a database read: this runs on every
    request in local development, and it is a constant.
    """
    set_tenant_id(str(DEFAULT_TENANT_ID))
    return {
        "tenant_id": DEFAULT_TENANT_ID,
        "slug": "default",
        "tenant_name": "Default tenant",
        "plan": "free",
        "status": "active",
        "rpm_limit": None,
        "rpd_limit": None,
        "key_name": "anonymous",
        "key_prefix": "-",
        "id": None,
    }
