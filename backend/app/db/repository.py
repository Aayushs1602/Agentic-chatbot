"""Data access for sessions, messages, artifacts, and tool calls.

Every read is scoped by `session_id`. That is the mechanism enforcing session
isolation — the requirement that each chat keeps independent context — and
`test_sessions.py` asserts no cross-session leakage rather than trusting it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from app.db import pool as db
from app.errors import NotFoundError
from app.logging import get_logger
from app.providers.base import Message

log = get_logger("repository")


# ── Sessions ────────────────────────────────────────────────────────────


async def create_session(
    *,
    title: str = "New chat",
    user_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO sessions (title, user_id, provider, model, metadata)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        RETURNING id, title, user_id, provider, model, created_at, updated_at, metadata
        """,
        title, user_id, provider, model, metadata or {},
    )
    log.info("session_created", session_id=str(row["id"]))
    return _session_row(row)


async def list_sessions(*, user_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT s.id, s.title, s.user_id, s.provider, s.model,
               s.created_at, s.updated_at, s.metadata,
               (SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS message_count
        FROM sessions s
        WHERE ($1::text IS NULL OR s.user_id = $1)
        ORDER BY s.updated_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )
    return [_session_row(r) for r in rows]


async def get_session(session_id: UUID) -> Dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, title, user_id, provider, model, created_at, updated_at, metadata "
        "FROM sessions WHERE id = $1",
        session_id,
    )
    if row is None:
        raise NotFoundError(f"No session {session_id}.")
    return _session_row(row)


async def rename_session(session_id: UUID, title: str) -> Dict[str, Any]:
    row = await db.fetchrow(
        "UPDATE sessions SET title = $2, updated_at = now() WHERE id = $1 "
        "RETURNING id, title, user_id, provider, model, created_at, updated_at, metadata",
        session_id, title[:200],
    )
    if row is None:
        raise NotFoundError(f"No session {session_id}.")
    return _session_row(row)


async def delete_session(session_id: UUID) -> None:
    # Messages, artifacts, and tool_calls cascade.
    result = await db.execute("DELETE FROM sessions WHERE id = $1", session_id)
    if result.endswith(" 0"):
        raise NotFoundError(f"No session {session_id}.")
    log.info("session_deleted", session_id=str(session_id))


async def touch_session(session_id: UUID) -> None:
    await db.execute("UPDATE sessions SET updated_at = now() WHERE id = $1", session_id)


async def autotitle_session(session_id: UUID, first_message: str) -> Optional[str]:
    """Name a session after its first question, if still untitled.

    A deterministic truncation rather than a model call: a title is not worth a
    second inference on a 3B, and this is predictable.
    """
    title = " ".join(first_message.split())[:60].rstrip()
    if len(first_message) > 60:
        title += "…"
    row = await db.fetchrow(
        "UPDATE sessions SET title = $2, updated_at = now() "
        "WHERE id = $1 AND title = 'New chat' RETURNING title",
        session_id, title or "New chat",
    )
    return row["title"] if row else None


def _session_row(row) -> Dict[str, Any]:
    out = {
        "id": str(row["id"]),
        "title": row["title"],
        "user_id": row["user_id"],
        "provider": row["provider"],
        "model": row["model"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "metadata": row["metadata"] or {},
    }
    if "message_count" in row:
        out["message_count"] = row["message_count"]
    return out


# ── Messages ────────────────────────────────────────────────────────────


async def add_message(
    session_id: UUID,
    *,
    role: str,
    content: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    intent: Optional[str] = None,
    latency_ms: Optional[int] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    citations: Optional[List[Dict[str, Any]]] = None,
    finish_reason: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO messages (session_id, role, content, provider, model, intent,
                              latency_ms, tokens_in, tokens_out, citations,
                              finish_reason, error)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb)
        RETURNING id, session_id, role, content, created_at, provider, model, intent,
                  latency_ms, tokens_in, tokens_out, citations, finish_reason, error
        """,
        session_id, role, content, provider, model, intent,
        latency_ms, tokens_in, tokens_out, citations or [], finish_reason, error,
    )
    await touch_session(session_id)
    return _message_row(row)


async def list_messages(session_id: UUID, *, limit: int = 200) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT id, session_id, role, content, created_at, provider, model, intent,
               latency_ms, tokens_in, tokens_out, citations, finish_reason, error
        FROM messages WHERE session_id = $1 ORDER BY created_at ASC LIMIT $2
        """,
        session_id, limit,
    )
    return [_message_row(r) for r in rows]


async def history_for_prompt(session_id: UUID, *, turns: int = 6) -> List[Message]:
    """Recent turns as provider Messages.

    Fetches the newest N and reverses, so a long session costs the same as a
    short one. Errored assistant rows are excluded — replaying a failure into
    the next prompt teaches the model to fail the same way.
    """
    rows = await db.fetch(
        """
        SELECT role, content FROM messages
        WHERE session_id = $1 AND content <> '' AND error IS NULL
        ORDER BY created_at DESC LIMIT $2
        """,
        session_id, turns,
    )
    return [Message(role=r["role"], content=r["content"]) for r in reversed(rows)]


def _message_row(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"].isoformat(),
        "provider": row["provider"],
        "model": row["model"],
        "intent": row["intent"],
        "latency_ms": row["latency_ms"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "citations": row["citations"] or [],
        "finish_reason": row["finish_reason"],
        "error": row["error"],
    }


# ── Tool calls ──────────────────────────────────────────────────────────


async def record_tool_calls(session_id: UUID, message_id: UUID, calls: List[Any]) -> None:
    """Persist the turn's tool trace.

    Best-effort: losing observability must never fail a turn the user already
    received an answer for.
    """
    if not calls:
        return
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO tool_calls (message_id, session_id, name, args,
                                        result_summary, duration_ms, ok, error)
                VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8)
                """,
                [
                    (message_id, session_id, c.name, c.args, c.result_summary,
                     c.duration_ms, c.ok, c.error)
                    for c in calls
                ],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("tool_calls_not_recorded", error=str(exc))


async def list_tool_calls(message_id: UUID) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        "SELECT name, args, result_summary, duration_ms, ok, error, created_at "
        "FROM tool_calls WHERE message_id = $1 ORDER BY created_at ASC",
        message_id,
    )
    return [
        {
            "name": r["name"],
            "args": r["args"],
            "result_summary": r["result_summary"],
            "duration_ms": r["duration_ms"],
            "ok": r["ok"],
            "error": r["error"],
        }
        for r in rows
    ]


# ── Artifacts ───────────────────────────────────────────────────────────


async def add_artifact(
    session_id: UUID,
    *,
    message_id: Optional[UUID],
    kind: str,
    title: str,
    content_raw: str,
    content_sanitized: str,
    sanitizer_report: Dict[str, Any],
) -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO artifacts (session_id, message_id, kind, title,
                               content_raw, content_sanitized, sanitizer_report)
        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
        RETURNING id, session_id, message_id, kind, title, content_raw,
                  content_sanitized, sanitizer_report, version, created_at
        """,
        session_id, message_id, kind, title[:200],
        content_raw, content_sanitized, sanitizer_report,
    )
    log.info("artifact_stored", artifact_id=str(row["id"]), kind=kind)
    return _artifact_row(row)


async def get_artifact(artifact_id: UUID) -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT id, session_id, message_id, kind, title, content_raw,
               content_sanitized, sanitizer_report, version, created_at
        FROM artifacts WHERE id = $1
        """,
        artifact_id,
    )
    if row is None:
        raise NotFoundError(f"No artifact {artifact_id}.")
    return _artifact_row(row)


async def list_artifacts(session_id: UUID) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT id, session_id, message_id, kind, title, content_raw,
               content_sanitized, sanitizer_report, version, created_at
        FROM artifacts WHERE session_id = $1 ORDER BY created_at ASC
        """,
        session_id,
    )
    return [_artifact_row(r) for r in rows]


def _artifact_row(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "message_id": str(row["message_id"]) if row["message_id"] else None,
        "kind": row["kind"],
        "title": row["title"],
        # `content_raw` is served only by the explicit /raw endpoint, so a
        # listing can never accidentally hand unsanitized HTML to the client.
        "content": row["content_sanitized"],
        "sanitizer_report": row["sanitizer_report"] or {},
        "version": row["version"],
        "created_at": row["created_at"].isoformat(),
    }


async def get_artifact_raw(artifact_id: UUID) -> str:
    """Original model output, for the viewer's source tab."""
    raw = await db.fetchval("SELECT content_raw FROM artifacts WHERE id = $1", artifact_id)
    if raw is None:
        raise NotFoundError(f"No artifact {artifact_id}.")
    return raw
