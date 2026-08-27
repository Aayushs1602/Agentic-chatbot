"""FastAPI application factory."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, search
from app.config import settings
from app.db import pool as db
from app.db.migrate import run_migrations
from app.errors import register_exception_handlers
from app.logging import configure_logging, get_logger, new_request_id, set_request_id

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
    except Exception as exc:  # noqa: BLE001
        log.error(
            "startup_migrations_failed",
            error=str(exc),
            hint="The API will start in a degraded state; see /readyz.",
        )
    yield
    await db.close_pool()
    log.info("shutdown")


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

    app.include_router(health.router)
    app.include_router(search.router, prefix="/api")

    return app


app = create_app()
