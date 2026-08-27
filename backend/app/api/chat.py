"""Sessions and the streaming chat endpoint.

The SSE event contract is a small closed set, so the frontend never parses a
provider's response shape:

    meta       once, first — provider, model, whether fallback fired
    tool       an agent step ran (routing, retrieval, relevance, skill)
    token      incremental text
    replace    discard what streamed; render this instead
    citations  resolved sources for the answer
    done       message id, usage, finish reason
    error      terminal failure, in the standard error envelope

`replace` exists because grounding can only be judged after generation finishes,
by which point tokens have already reached the client. Rather than show a
plausible uncited answer, the orchestrator replaces it — and the UI is told to.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.orchestrator import Orchestrator, TurnResult, _ReplaceText
from app.db import repository as repo
from app.errors import AppError
from app.logging import get_logger, get_request_id
from app.providers.base import Completed, StreamError, TextDelta, ToolEvent
from app.providers.registry import get_registry

log = get_logger("api.chat")
router = APIRouter(tags=["chat"])

_USER_COOKIE = "lga_uid"


# ── Contracts ───────────────────────────────────────────────────────────


class CreateSession(BaseModel):
    title: str = Field("New chat", max_length=200)


class RenameSession(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class SendMessage(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    provider: Optional[str] = Field(None, description="Override the active provider")


# ── Sessions ────────────────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_201_CREATED, summary="Start a new chat")
async def create_session(body: CreateSession, request: Request, response: Response):
    registry = get_registry()
    user_id = _user_id(request, response)
    session = await repo.create_session(
        title=body.title,
        user_id=user_id,
        provider=registry.active_id,
        model=registry.get(registry.active_id).model,
        metadata={
            "user_agent": request.headers.get("user-agent", "")[:300],
            "accept_language": request.headers.get("accept-language", "")[:100],
        },
    )
    return session


@router.get("/sessions", summary="List chats")
async def list_sessions(request: Request, response: Response, limit: int = Query(50, ge=1, le=200)):
    return {"sessions": await repo.list_sessions(user_id=_user_id(request, response), limit=limit)}


@router.get("/sessions/{session_id}", summary="Get one chat")
async def get_session(session_id: UUID):
    return await repo.get_session(session_id)


@router.patch("/sessions/{session_id}", summary="Rename a chat")
async def rename_session(session_id: UUID, body: RenameSession):
    return await repo.rename_session(session_id, body.title)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: UUID) -> Response:
    await repo.delete_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", summary="Chat history")
async def list_messages(session_id: UUID):
    await repo.get_session(session_id)  # 404 rather than an empty list
    return {"messages": await repo.list_messages(session_id)}


# ── Streaming chat ──────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/messages", summary="Send a message (SSE stream)")
async def send_message(session_id: UUID, body: SendMessage, request: Request):
    await repo.get_session(session_id)

    # Resolve the provider before streaming starts: while headers can still be
    # sent, an unavailable provider is a clean 503 instead of an error event
    # inside a 200 response.
    registry = get_registry()
    provider, fell_back_from = await registry.resolve(body.provider)

    await repo.add_message(session_id, role="user", content=body.content)
    await repo.autotitle_session(session_id, body.content)
    history = await repo.history_for_prompt(session_id, turns=6)
    # Drop the just-stored turn; the orchestrator passes the question separately.
    history = [m for m in history if m.content != body.content or m.role != "user"]

    return StreamingResponse(
        _event_stream(session_id, body.content, history, provider, fell_back_from, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx would otherwise buffer the stream
            "X-Request-ID": get_request_id(),
        },
    )


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(
    session_id: UUID,
    question: str,
    history: List[Any],
    provider: Any,
    fell_back_from: Optional[str],
    request: Request,
) -> AsyncIterator[str]:
    result = TurnResult()
    orchestrator = Orchestrator(provider, fell_back_from=fell_back_from)

    yield _sse("meta", {
        "provider": provider.id,
        "model": provider.model,
        "fell_back_from": fell_back_from,
        "request_id": get_request_id(),
    })

    try:
        async for event in orchestrator.run(question, history=history, result=result):
            # Stop generating when the browser goes away — otherwise an
            # abandoned tab keeps a 4GB GPU busy for a minute.
            if await request.is_disconnected():
                log.info("client_disconnected", session_id=str(session_id))
                break

            if isinstance(event, TextDelta):
                yield _sse("token", {"text": event.text})
            elif isinstance(event, _ReplaceText):
                yield _sse("replace", {"text": event.text})
            elif isinstance(event, ToolEvent):
                yield _sse("tool", {
                    "name": event.name,
                    "summary": event.result_summary,
                    "ok": event.ok,
                })
            elif isinstance(event, StreamError):
                yield _sse("error", {"error": {
                    "code": event.code,
                    "message": event.message,
                    "detail": event.detail,
                    "request_id": get_request_id(),
                }})
            elif isinstance(event, Completed):
                result.finish_reason = event.finish_reason

        if result.citations:
            yield _sse("citations", {"citations": result.citations})

        message = await _persist(session_id, result)
        yield _sse("done", {
            "message_id": message["id"],
            "intent": result.intent,
            "abstained": result.abstained,
            "finish_reason": result.finish_reason,
            "latency_ms": result.latency_ms,
            "usage": {"tokens_in": result.usage.tokens_in,
                      "tokens_out": result.usage.tokens_out},
        })

    except AppError as exc:
        log.warning("chat_stream_app_error", code=exc.code, message=exc.message)
        await _persist(session_id, result, error={"code": exc.code, "message": exc.message})
        yield _sse("error", {"error": {
            "code": exc.code, "message": exc.message,
            "detail": exc.detail, "request_id": get_request_id(),
        }})
    except Exception as exc:  # noqa: BLE001
        log.exception("chat_stream_failed", error=str(exc))
        await _persist(session_id, result, error={"code": "internal_error", "message": str(exc)[:300]})
        yield _sse("error", {"error": {
            "code": "internal_error",
            "message": "The assistant failed mid-response.",
            "detail": {"hint": "Check the backend logs for this request_id."},
            "request_id": get_request_id(),
        }})


async def _persist(
    session_id: UUID, result: TurnResult, *, error: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Store the assistant turn. Partial answers are kept, not discarded."""
    message = await repo.add_message(
        session_id,
        role="assistant",
        content=result.text,
        provider=result.provider_id,
        model=result.model,
        intent=result.intent or None,
        latency_ms=result.latency_ms,
        tokens_in=result.usage.tokens_in or None,
        tokens_out=result.usage.tokens_out or None,
        citations=result.citations,
        finish_reason=result.finish_reason,
        error=error or result.error,
    )
    await repo.record_tool_calls(session_id, UUID(message["id"]), result.tool_calls)
    return message


def _user_id(request: Request, response: Response) -> str:
    """Anonymous per-browser id.

    No auth by design — this is an internal single-team tool, and the brief asks
    for user metadata, not accounts. A cookie is enough to keep one person's
    session list to themselves, and it is not a security boundary.
    """
    existing = request.cookies.get(_USER_COOKIE)
    if existing:
        return existing
    import uuid

    fresh = uuid.uuid4().hex
    response.set_cookie(
        _USER_COOKIE, fresh, max_age=60 * 60 * 24 * 365,
        httponly=True, samesite="lax",
    )
    return fresh
