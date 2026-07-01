"""Request body-size guard middleware.

A Starlette ``BaseHTTPMiddleware`` that rejects any request whose body exceeds
``settings.max_request_body_bytes`` with a Contract-2 **413 PAYLOAD_TOO_LARGE**
envelope BEFORE the route handler (and therefore before the JSON body is parsed).

Enforcement point: **Content-Length** — when the client sends it, reject up front
without reading a byte. (Wrapping the ASGI receive channel inside a
``BaseHTTPMiddleware`` corrupts downstream body parsing, so it is intentionally not
done; uvicorn enforces its own ceiling for the chunked-and-lying case.)

The middleware emits the SAME Contract-2 error envelope as the exception handlers
directly, because a middleware runs OUTSIDE the FastAPI exception-handler stack.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import trace

logger = structlog.get_logger(__name__)

_CODE_PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _too_large_response(limit: int, observed: int | None) -> JSONResponse:
    """Render the Contract-2 413 envelope (matches core.errors._render shape)."""
    details: dict[str, object] = {"reason": "BODY_BYTES_EXCEEDED", "max_bytes": limit}
    if observed is not None:
        details["bytes"] = observed
    body = {
        "error": {
            "code": _CODE_PAYLOAD_TOO_LARGE,
            "message": f"Request body exceeds the maximum of {limit} bytes.",
            "details": details,
            "request_id": trace.request_id_var.get(),
            "trace_id": trace.trace_id_var.get(),
            "timestamp": _now_iso(),
        }
    }
    return JSONResponse(status_code=413, content=body)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds the configured byte cap (413)."""

    def __init__(self, app: object, *, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limit = self._max_bytes
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > limit:
                logger.info(
                    "body_too_large", source="content_length", bytes=declared, max_bytes=limit
                )
                return _too_large_response(limit, declared)
        return await call_next(request)
