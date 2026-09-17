"""Usage and cost for the calling tenant.

There is deliberately **no `/usage/{tenant_id}`**. The roadmap asked for one,
and P1's own RLS work made it impossible to implement honestly: the pool
authenticates as a role that cannot see another tenant's rows, so such an
endpoint would either return an empty result (confusing) or require bypassing
the isolation this phase exists to enforce (worse).

Cross-tenant reporting is a real need and it belongs to a different actor — an
operator connecting with owner credentials — not to an API surface reachable
with a tenant's own key. When that arrives it should be a separate service or
an explicitly elevated role, not a path parameter here.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_tenant
from app.db import usage as usage_repo

router = APIRouter(tags=["usage"])


@router.get("/usage", summary="Token and cost totals for this tenant")
async def get_usage(
    since: Optional[date] = Query(None, description="Inclusive start date (UTC)"),
    until: Optional[date] = Query(None, description="Inclusive end date (UTC)"),
    principal: Dict[str, Any] = Depends(require_tenant),
) -> Dict[str, Any]:
    """Totals plus a per-model breakdown. Defaults to the last 30 days.

    `cost_complete` is the field to read before trusting `totals.cost_micros`:
    it is false when some model in the window has no configured rate, meaning
    real tokens were spent that the total does not account for.
    """
    summary = await usage_repo.summary(since=since, until=until)
    return {"tenant": principal.get("slug"), **summary}


@router.get("/usage/daily", summary="Per-day usage series for this tenant")
async def get_usage_daily(
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    _: Dict[str, Any] = Depends(require_tenant),
) -> Dict[str, List[Dict[str, Any]]]:
    return {"days": await usage_repo.daily(since=since, until=until)}
