"""API key management.

A tenant manages its own keys using a key it already holds — the same shape as
key rotation everywhere else: mint the new one, move traffic across, revoke the
old one, never having been unauthenticated in between.

Deliberately not in `admin.py`, which documents itself as read-only corpus
inspection. Mixing mutation into a router whose contract says it never mutates
is how that contract stops being true.

The first key is the bootstrap problem every system with keys has. Here it is
solved outside the API: `DEV_API_KEY` seeds the `dev` tenant at startup
(app/db/tenants.py), so the plaintext lives in an operator's environment rather
than in a migration, a fixture, or this file.
"""

from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.api.deps import require_tenant
from app.db import tenants as tenant_repo
from app.logging import get_logger

log = get_logger("api.keys")
router = APIRouter(tags=["keys"])


class CreateKey(BaseModel):
    name: str = Field("default", min_length=1, max_length=80)


@router.get("/keys", summary="List this tenant's API keys")
async def list_keys(principal: Dict[str, Any] = Depends(require_tenant)) -> Dict[str, Any]:
    # Prefixes and timestamps only — the hash is never returned, and there is
    # no endpoint anywhere that reveals a key after creation.
    return {"keys": await tenant_repo.list_api_keys(principal["tenant_id"])}


@router.post("/keys", status_code=status.HTTP_201_CREATED, summary="Mint a new API key")
async def create_key(
    body: CreateKey, principal: Dict[str, Any] = Depends(require_tenant)
) -> Dict[str, Any]:
    """Create a key for the calling tenant.

    The plaintext is in this response and nowhere else, ever. It is not stored,
    not logged, and not recoverable — losing it means minting another one.
    """
    generated, row = await tenant_repo.create_api_key(
        principal["tenant_id"], name=body.name
    )
    return {
        "key": row,
        "plaintext": generated.plaintext,
        "warning": "Store this now. It cannot be retrieved again.",
    }


@router.delete(
    "/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_key(
    key_id: UUID, principal: Dict[str, Any] = Depends(require_tenant)
) -> Response:
    # Scoped to the calling tenant in the query itself: without that, knowing
    # any key's UUID would be enough to revoke someone else's.
    await tenant_repo.revoke_api_key(principal["tenant_id"], key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/whoami", summary="Which tenant is this key for")
async def whoami(principal: Dict[str, Any] = Depends(require_tenant)) -> Dict[str, Any]:
    """The one-curl answer to "is my key working, and as whom".

    Worth its own endpoint: without it, the first thing anyone does with a new
    key is call a real endpoint and try to infer auth from the failure.
    """
    return {
        "tenant_id": str(principal["tenant_id"]),
        "slug": principal.get("slug"),
        "plan": principal.get("plan"),
        "key_name": principal.get("key_name"),
        "key_prefix": principal.get("key_prefix"),
    }
