"""Contract 2 error envelope + handlers."""

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


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_DEFAULT_STATUS = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_REVOKED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ApiError(Exception):
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


def render(code: str, message: str, status_code: int, details: dict[str, Any] | None = None,
           headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code, "message": message,
            "request_id": trace.request_id_var.get(), "trace_id": trace.trace_id_var.get(),
            "timestamp": _now_iso(),
        }
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_r: Request, exc: ApiError) -> JSONResponse:
        return render(exc.code, exc.message, exc.status_code, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _val(_r: Request, exc: RequestValidationError) -> JSONResponse:
        return render(
            ErrorCode.VALIDATION_ERROR, "Request validation failed.", 422,
            {"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: ErrorCode.UNAUTHORIZED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND,
                429: ErrorCode.RATE_LIMIT_EXCEEDED, 503: ErrorCode.SERVICE_UNAVAILABLE}.get(
            exc.status_code,
            ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR,
        )
        return render(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_r: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), exc_info=exc)
        return render(ErrorCode.INTERNAL_ERROR, "An internal error occurred.", 500)
