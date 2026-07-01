"""Contract 2 — canonical API error envelope.

Defines :class:`ApiError` plus the FastAPI exception handlers that render every
error as::

    { "error": { "code", "message", "details?", "request_id", "trace_id", "timestamp" } }

Error-code constants use the Contract 2 spelling. RAG adds two domain codes on top of
the shared reserved set: ``FORBIDDEN_KB`` (KB ACL deny) and ``QUOTA_EXCEEDED`` (a
Contract-19 limit breach — 413 for count/storage caps, 429 for rate windows).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import trace

logger = structlog.get_logger(__name__)


class ErrorCode:
    """Canonical reserved error codes (SCREAMING_SNAKE_CASE)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    # RAG-specific KB access-control deny (Component 5c). Renders as 403.
    FORBIDDEN_KB = "FORBIDDEN_KB"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    # Contract-19 quota breach. Status is chosen per breach: 413 (count/storage) or 429 (rate).
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    IDEMPOTENCY_REQUEST_IN_FLIGHT = "IDEMPOTENCY_REQUEST_IN_FLIGHT"
    TOKEN_REVOKED = "TOKEN_REVOKED"


# Default HTTP status per code (used when ApiError does not override).
_DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.FORBIDDEN_KB: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.QUOTA_EXCEEDED: 413,
    ErrorCode.BUDGET_EXCEEDED: 402,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
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


def parse_uuid(value: str, *, field: str = "id") -> str:
    """Validate that a path parameter is a well-formed UUID; return its canonical string.

    Path params bound to ``uuid`` columns must be UUIDs — a raw non-UUID string would surface
    as a Postgres ``invalid input syntax for type uuid`` error (HTTP 500) on every KB-scoped
    endpoint. Validating here turns that into the standard 422 ``VALIDATION_ERROR`` envelope.
    The canonical string form is returned so downstream string-keyed lookups are unchanged.
    """
    import uuid as _uuid

    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{field} must be a valid UUID.",
            status_code=422,
            details={"reason": "INVALID_UUID", "field": field},
        ) from exc


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
        # ``exc.errors()`` can carry non-JSON-serializable objects in ``ctx`` (e.g. the original
        # ValueError raised by a field_validator like UploadUrlRequest._no_path_traversal). If we
        # hand that straight to JSONResponse it raises a TypeError that falls through to the
        # catch-all handler and renders a misleading 500. jsonable_encoder makes it JSON-safe so
        # the proper 422 VALIDATION_ERROR envelope is returned (BUG 2).
        from fastapi.encoders import jsonable_encoder

        return _render(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            422,
            {"errors": jsonable_encoder(exc.errors())},
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
