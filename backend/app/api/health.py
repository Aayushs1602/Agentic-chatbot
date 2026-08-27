"""Liveness and readiness.

/healthz  — is the process up? (container healthcheck; never touches dependencies)
/readyz   — is it actually able to serve? Reports every dependency separately.

/readyz is the highest-leverage endpoint in the system for an operator: when
something is broken, one curl says which thing, and `degraded` names the exact
next action. Everything it reports is also what the UI's provider badge renders.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Response, status

from app.config import settings
from app.db import pool as db
from app.logging import get_logger
from app.providers.probe import probe_all

log = get_logger("health")
router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> Dict[str, Any]:
    # Intentionally dependency-free: if this checked Postgres, a database
    # outage would make Docker restart-loop a perfectly healthy container.
    return {
        "status": "ok",
        "version": settings.app_version,
        "env": settings.app_env,
        "uptime_s": round(time.monotonic() - _STARTED_AT, 1),
    }


@router.get("/readyz", summary="Readiness probe with per-dependency detail")
async def readyz(response: Response) -> Dict[str, Any]:
    degraded: List[str] = []

    # Probe every dependency concurrently: serially, a down database and a down
    # Ollama each cost their own timeout, and readiness ends up slower than the
    # thing it is reporting on.
    db_ok, providers = await asyncio.gather(db.ping(), probe_all())
    if not db_ok:
        degraded.append("database: not reachable — check `docker compose ps`")

    corpus: Dict[str, Any] = {"episodes": 0, "chunks": 0}
    if db_ok:
        try:
            row = await db.fetchrow(
                "SELECT (SELECT count(*) FROM episodes) AS episodes,"
                "       (SELECT count(*) FROM chunks)   AS chunks"
            )
            corpus = {"episodes": row["episodes"], "chunks": row["chunks"]}
        except Exception as exc:  # noqa: BLE001 — pre-migration is a normal state
            degraded.append("corpus: schema not migrated yet")
            log.warning("readyz_corpus_failed", error=str(exc))
    if corpus["chunks"] == 0 and db_ok and "corpus: schema not migrated yet" not in degraded:
        degraded.append("corpus: empty — run `make ingest LIMIT=20`")

    active = next((p for p in providers if p["id"] == settings.llm_provider), None)
    if active is None:
        degraded.append(f"provider: unknown provider '{settings.llm_provider}'")
    elif not active["available"]:
        degraded.append(f"provider: {settings.llm_provider} unavailable — {active['reason']}")

    embeddings = {
        "provider": settings.embeddings_provider,
        "model": settings.embeddings_model,
        "dim": settings.embeddings_dim,
    }

    ready = db_ok and not any(d.startswith(("database", "corpus: schema")) for d in degraded)
    # 503 only for hard failures. An empty corpus or an unavailable provider is
    # degraded, not down — the app still serves, and saying so keeps the signal
    # useful instead of making every readiness check red.
    response.status_code = (
        status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return {
        "status": "ready" if ready and not degraded else ("degraded" if ready else "not_ready"),
        "version": settings.app_version,
        "database": {"reachable": db_ok, "url": _redact_dsn(settings.database_url)},
        "embeddings": embeddings,
        "corpus": corpus,
        "provider": {"active": settings.llm_provider, "fallback": settings.provider_fallback},
        "providers": providers,
        "degraded": degraded,
    }


def _redact_dsn(dsn: str) -> str:
    """postgresql://user:pw@host:5432/db -> postgresql://user:***@host:5432/db"""
    try:
        scheme, rest = dsn.split("://", 1)
        if "@" not in rest:
            return dsn
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    except Exception:  # noqa: BLE001
        return "***"
