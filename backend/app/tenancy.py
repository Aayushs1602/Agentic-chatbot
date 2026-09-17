"""The current tenant, as a typed value.

`app.logging` owns the ContextVar itself, because every log line carries it.
This module is the typed accessor on top: it turns the string form into a UUID
and refuses to guess when there isn't one.

Two things populate it, and only two:

* `api/deps.require_tenant` — an authenticated HTTP request.
* a script that sets it deliberately (see `rag/ingest.py`), because a CLI run
  has no request to inherit from.

Anything else reaching a write path without a tenant is a bug, not a user
error, which is why `current_tenant()` raises rather than falling back to a
default. A silent fallback here would write one tenant's data into another's
account, and nothing downstream would ever flag it.
"""

from __future__ import annotations

from uuid import UUID

from app.errors import AppError
from app.logging import get_tenant_id, set_tenant_id

__all__ = ["current_tenant", "current_tenant_or_none", "set_tenant_id", "TenantRequiredError"]


class TenantRequiredError(AppError):
    code = "tenant_required"
    status_code = 500
    message = "No tenant is bound to this operation."


def current_tenant() -> UUID:
    """The tenant for this request or script run.

    500, not 401: an unauthenticated *request* is rejected by `require_tenant`
    long before it reaches a repository call. Getting here means a write path
    was invoked with no tenant bound, which is a wiring mistake in our code.
    """
    raw = get_tenant_id()
    if not raw or raw == "-":
        raise TenantRequiredError(
            detail={
                "hint": "A write reached the database with no tenant bound. "
                        "HTTP handlers must depend on require_tenant; scripts "
                        "must call set_tenant_id() before writing."
            }
        )
    try:
        return UUID(raw)
    except ValueError as exc:
        raise TenantRequiredError(
            detail={"hint": f"Tenant {raw!r} is not a UUID."}
        ) from exc


def current_tenant_or_none() -> UUID | None:
    """For read paths that are legitimately tenant-less (health, migrations)."""
    try:
        return current_tenant()
    except TenantRequiredError:
        return None
