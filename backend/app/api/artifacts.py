"""Artifact retrieval.

Two endpoints with deliberately different contracts:

* the default returns **sanitized** content plus the sanitizer report — this is
  what the viewer renders;
* `/raw` returns what the model actually produced, for the "view source" tab.

Keeping them apart means no listing or default fetch can hand unsanitized HTML
to a client by accident. `/raw` is served as `text/plain` so a browser opening
the URL directly can never execute it.
"""

from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Response

from app.db import repository as repo

router = APIRouter(tags=["artifacts"])


@router.get("/sessions/{session_id}/artifacts", summary="Artifacts in a chat")
async def list_artifacts(session_id: UUID) -> Dict[str, Any]:
    await repo.get_session(session_id)
    return {"artifacts": await repo.list_artifacts(session_id)}


@router.get("/artifacts/{artifact_id}", summary="One artifact (sanitized)")
async def get_artifact(artifact_id: UUID) -> Dict[str, Any]:
    return await repo.get_artifact(artifact_id)


@router.get("/artifacts/{artifact_id}/raw", summary="Original model output")
async def get_artifact_raw(artifact_id: UUID) -> Response:
    artifact = await repo.get_artifact(artifact_id)
    row = await repo.get_artifact_raw(artifact_id)
    return Response(
        content=row,
        # text/plain, never text/html: opening this URL directly must display
        # the source, not render it.
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{artifact["id"]}.txt"',
            "X-Content-Type-Options": "nosniff",
        },
    )
