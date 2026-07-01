"""Contract 2 — canonical API error envelope.

Defines :class:`ApiError` plus the FastAPI exception handlers that render every
error as::

    { "error": { "code", "message", "details?", "request_id", "trace_id", "timestamp" } }

Error-code constants use the Contract 2 spelling (x-known-codes in
contracts/api/error-format.schema.json). Note in particular the IDEMPOTENCY_*
spelling (NOT IDEMPOTENT_*).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import trace

logger = structlog.get_logger(__name__)


# ── Error-code constants (Contract 2 x-known-codes spelling) ───────────────────
class ErrorCode:
    """Canonical reserved error codes (SCREAMING_SNAKE_CASE)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    IDEMPOTENCY_REQUEST_IN_FLIGHT = "IDEMPOTENCY_REQUEST_IN_FLIGHT"
    TOKEN_REVOKED = "TOKEN_REVOKED"


# Default HTTP status per code (used when ApiError does not override).
_DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.BUDGET_EXCEEDED: 402,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.MODEL_UNSUPPORTED: 422,
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_REQUEST_IN_FLIGHT: 409,
    ErrorCode.TOKEN_REVOKED: 401,
}


class ApiError(Exception):
    """An error that renders to the Contract 2 envelope with a chosen HTTP status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code if status_code is not None else _DEFAULT_STATUS.get(code, 500)
        self.details = details
        self.headers = headers


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _render(
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": trace.request_id_var.get(),
            "trace_id": trace.trace_id_var.get(),
            "timestamp": _now_iso(),
        }
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the Contract 2 exception handlers on the FastAPI app."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("api_error", code=exc.code, message=exc.message, status=exc.status_code)
        else:
            logger.info("api_error", code=exc.code, message=exc.message, status=exc.status_code)
        return _render(exc.code, exc.message, exc.status_code, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() can carry non-JSON-serializable objects under each error's
        # ctx.error (e.g. the raw ValueError raised by a custom field_validator such as
        # `stop` >4 or the tool function.name regex). Passing those straight into
        # JSONResponse -> json.dumps would crash the 422 handler itself, surfacing a
        # generic 500 with no detail. jsonable_encoder coerces them to plain JSON
        # (ValueError -> its str()) so we always render a proper Contract-2 422.
        errors = jsonable_encoder(exc.errors())
        return _render(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            422,
            {"errors": errors},
            None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        default = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR
        code = {
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            429: ErrorCode.RATE_LIMIT_EXCEEDED,
            503: ErrorCode.SERVICE_UNAVAILABLE,
        }.get(exc.status_code, default)
        return _render(code, str(exc.detail), exc.status_code, None, None)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), exc_info=exc)
        return _render(ErrorCode.INTERNAL_ERROR, "An internal error occurred.", 500, None, None)
