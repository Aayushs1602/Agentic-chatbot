"""Structured errors.

Every non-2xx response — including unhandled exceptions and validation
failures — is returned in one envelope:

    {"error": {"code", "message", "detail", "request_id"}}

`code` is a stable machine-readable string the frontend switches on; `message`
is shown to the user; `detail.hint` carries the operator-facing next step
("Run `ollama serve`"), which is what turns a failure into a fixable one.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger, get_request_id

log = get_logger("errors")


class AppError(Exception):
    """Base for every expected failure. Unexpected ones become `internal_error`."""

    code = "internal_error"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "Something went wrong."

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        detail: Optional[Dict[str, Any]] = None,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        self.message = message or self.message
        self.detail = detail or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        return error_response(
            code=self.code,
            message=self.message,
            detail=self.detail,
            status_code=self.status_code,
        )


class NotFoundError(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ValidationError(AppError):
    code = "validation_error"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "The request was not valid."


class DatabaseUnavailableError(AppError):
    code = "database_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "The database is not reachable."


class ProviderUnavailableError(AppError):
    code = "provider_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "The selected model provider is not reachable."


class ProviderTimeoutError(AppError):
    code = "provider_timeout"
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    message = "The model took too long to respond."


class CorpusEmptyError(AppError):
    code = "corpus_empty"
    status_code = status.HTTP_409_CONFLICT
    message = "No transcripts have been ingested yet."

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("detail", {"hint": "Run `make ingest LIMIT=20` to load transcripts."})
        super().__init__(**kw)


def error_response(
    *,
    code: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
    status_code: int = 500,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "detail": detail or {},
                "request_id": get_request_id(),
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        # Expected failures are warnings, not errors — they don't need a stack trace.
        log.warning("app_error", code=exc.code, message=exc.message, detail=exc.detail)
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            code="validation_error",
            message="The request was not valid.",
            detail={"errors": exc.errors()},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized"}
        return error_response(
            code=codes.get(exc.status_code, "http_error"),
            message=str(exc.detail),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path, error=str(exc))
        # Never leak internals to the client; the request_id ties this response
        # to the full stack trace in the logs.
        return error_response(
            code="internal_error",
            message="An unexpected error occurred.",
            detail={"hint": "Check the backend logs for this request_id."},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
