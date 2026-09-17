"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, artifacts, chat, health, keys, providers, search
from app.api.deps import require_tenant
from app.config import settings
from app.db import pool as db
from app.agent.skills import get_skills
from app.db.migrate import provision_app_role, run_migrations
from app.db.tenants import bootstrap_dev_key
from app.providers.registry import get_registry
from app.errors import register_exception_handlers
from app.logging import (
    configure_logging,
    get_logger,
    new_request_id,
    set_request_id,
    set_tenant_id,
)

configure_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "startup",
        env=settings.app_env,
        version=settings.app_version,
        provider=settings.llm_provider,
        model=settings.ollama_model,
        embeddings=settings.embeddings_model,
    )
    # Migrations run here rather than in the Dockerfile CMD so there is one code
    # path whether the app starts under Compose, bare uvicorn, or a test fixture.
    # A DB that is down must NOT prevent startup — otherwise /readyz can't report it.
    try:
        await run_migrations()
        # Gives `lenny_app` a password so the pool can authenticate as it.
        # After migrations, because 003_rls creates the role.
        await provision_app_role()
        # Seeds DEV_API_KEY against the `dev` tenant when set; no-op otherwise.
        # After migrations, because it needs the tenant row 002 creates.
        await bootstrap_dev_key()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "startup_migrations_failed",
            error=str(exc),
            hint="The API will start in a degraded state; see /readyz.",
        )

    if settings.running_as_owner:
        # The single most important line in this log. Every policy in
        # 003_rls.sql exists and is tested, and none of them apply: the pool is
        # authenticating as a superuser that owns the tables, and both of those
        # bypass RLS unconditionally. Tenant isolation is NOT being enforced.
        log.warning(
            "rls_not_enforced",
            hint="APP_DATABASE_URL is unset, so the app connects as the owner. "
                 "Set APP_DB_PASSWORD and APP_DATABASE_URL to enforce RLS.",
        )

    if not settings.auth_required:
        # Loud on purpose. An unauthenticated API is a legitimate local
        # configuration and an incident anywhere else, and the difference
        # should be visible in the first ten lines of the log.
        log.warning(
            "auth_disabled",
            hint="AUTH_REQUIRED=false — every request runs as the default tenant.",
        )
    # Load skills eagerly so a malformed SKILL.md is a startup log line rather
    # than a surprise on the first request that needs it.
    get_skills().load()

    # Warm the model in the background: cold-loading the 3B measured ~77s on the
    # target GPU, and the first user request should not be the one that pays it.
    # Backgrounded so a slow or absent Ollama never delays startup.
    asyncio.create_task(_warmup())

    yield
    await db.close_pool()
    log.info("shutdown")


async def _warmup() -> None:
    try:
        await get_registry().warmup_active()
    except Exception as exc:  # noqa: BLE001
        log.debug("warmup_failed", error=str(exc))


def create_app() -> FastAPI:
    app = FastAPI(
        title="The Lenny Growth Assistant",
        description=(
            "A grounded assistant over Lenny's Podcast transcripts. "
            "Answers cite their sources; when the corpus doesn't cover a "
            "question, it says so instead of guessing."
        ),
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Bind a request id to every log line and echo it back to the client.

        The id in an error envelope is the same id in the logs, which is what
        makes a user-reported failure traceable without reproducing it.
        """
        rid = request.headers.get("X-Request-ID") or new_request_id()
        set_request_id(rid)
        # Cleared at the start of every request, not just set at auth time.
        # Task-local ContextVars should already isolate requests, but "should"
        # is the wrong level of confidence for the value that is about to drive
        # `SET LOCAL app.tenant_id`: a stale tenant leaking into an
        # unauthenticated request would be an isolation failure, not a log typo.
        set_tenant_id("-")
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        # /healthz is polled every 10s by Docker; logging it drowns everything else.
        if request.url.path != "/healthz":
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=elapsed_ms,
            )
        return response

    register_exception_handlers(app)

    # Routers that touch tenant data authenticate at the router level, not
    # per-handler: a new endpoint added to one of these files is protected by
    # default, and protecting it cannot be forgotten. The dependency also binds
    # the tenant ContextVar that db/pool.py reads for `SET LOCAL app.tenant_id`,
    # so authentication and row visibility come from the same decision.
    tenant_scoped = [Depends(require_tenant)]

    # Open: no tenant data. /healthz and /readyz must answer while the database
    # is down, which is exactly when auth cannot be checked; provider status is
    # process-level, identical for every tenant, and drives the UI badge.
    app.include_router(health.router)
    app.include_router(providers.router, prefix="/api")

    app.include_router(search.router, prefix="/api", dependencies=tenant_scoped)
    app.include_router(chat.router, prefix="/api", dependencies=tenant_scoped)
    app.include_router(artifacts.router, prefix="/api", dependencies=tenant_scoped)
    app.include_router(admin.router, prefix="/api", dependencies=tenant_scoped)
    # keys.py declares require_tenant per route, since it also reads the
    # principal to know whose keys to list.
    app.include_router(keys.router, prefix="/api")

    return app


app = create_app()
