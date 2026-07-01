"""Contract 2 — canonical API error envelope.

Defines :class:`ApiError` plus the FastAPI exception handlers that render every
error as::

    { "error": { "code", "message", "details?", "request_id", "trace_id", "timestamp" } }

Error-code constants use the Contract 2 spelling (x-known-codes in
contracts/api/error-format.schema.json). The xAgent first-cycle status mapping per
the spec:

  * ``GUARDRAIL_VIOLATION`` -> 422
  * ``VALIDATION_ERROR``    -> 422
  * ``NOT_FOUND``           -> 404
  * ``UNAUTHORIZED``        -> 401
  * ``CONFLICT``            -> 409 (agent runtime not configured)
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


# ── Error-code constants (Contract 2 x-known-codes spelling) ───────────────────
class ErrorCode:
    """Canonical reserved error codes (SCREAMING_SNAKE_CASE)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    # Live token revocation (Component 3c, WP03) — a token that passed signature/claims
    # but was killed verifier-side (revoked jti, poisoned signing kid, or an agent
    # revoke-all epoch newer than the token's iat). Reserved in Contract 2; 401.
    TOKEN_REVOKED = "TOKEN_REVOKED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    GUARDRAIL_VIOLATION = "GUARDRAIL_VIOLATION"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Surfaced upstream from the llms-gateway (BUG 2): the agent's configured model is not
    # supported / not allowed for this request. A client/config error (422), NOT an
    # availability error — kept distinct so it is never masked as SERVICE_UNAVAILABLE.
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"


# Default HTTP status per code (used when ApiError does not override).
# Note the xAgent first-cycle mapping: GUARDRAIL_VIOLATION and VALIDATION_ERROR
# are BOTH 422 (Contract 3 A2A parity / Component 2).
_DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_REVOKED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.GUARDRAIL_VIOLATION: 422,
    ErrorCode.BUDGET_EXCEEDED: 402,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.MODEL_UNSUPPORTED: 422,
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
        # xAgent renders pydantic body-validation failures as 422 VALIDATION_ERROR
        # (Contract 3 A2A parity — over-cap input, bad timeout, etc. share this status).
        return _render(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            422,
            {"errors": exc.errors()},
            None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        default = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR
        code = {
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.CONFLICT,
            429: ErrorCode.RATE_LIMIT_EXCEEDED,
            503: ErrorCode.SERVICE_UNAVAILABLE,
        }.get(exc.status_code, default)
        return _render(code, str(exc.detail), exc.status_code, None, None)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), exc_info=exc)
        return _render(ErrorCode.INTERNAL_ERROR, "An internal error occurred.", 500, None, None)
