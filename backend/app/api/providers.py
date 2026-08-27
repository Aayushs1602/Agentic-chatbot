"""Provider inspection and runtime switching.

Backs the UI's provider badge, which satisfies the brief's requirement that the
selected provider be visible and switchable without touching code. Every entry
carries `available` plus a `reason` and `hint` when it isn't, so the UI can
explain a greyed-out option rather than just greying it out.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.skills import get_skills
from app.config import settings
from app.providers.registry import get_registry

router = APIRouter(tags=["providers"])


class SetProvider(BaseModel):
    provider: str


@router.get("/providers", summary="List providers and their availability")
async def list_providers() -> Dict[str, Any]:
    registry = get_registry()
    statuses = await registry.statuses()
    return {
        "active": registry.active_id,
        "fallback_enabled": settings.provider_fallback,
        "fallback_order": settings.fallback_order,
        "providers": [s.to_dict() for s in statuses],
    }


@router.post("/providers/active", summary="Switch the active provider")
async def set_active(body: SetProvider) -> Dict[str, Any]:
    status = await get_registry().set_active(body.provider)
    return {"active": body.provider, "status": status.to_dict()}


@router.get("/skills", summary="List loaded skills")
async def list_skills() -> Dict[str, Any]:
    # Exposed because "which skills exist and when do they fire" is the first
    # question anyone asks about an agent, and reading it from the running
    # system beats reading it from a doc that may have drifted.
    return {"skills": get_skills().catalogue()}
