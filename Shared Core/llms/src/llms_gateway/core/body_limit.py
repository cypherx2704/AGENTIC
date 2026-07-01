"""Request body-size guard middleware (WP06 multimodal caps).

A Starlette ``BaseHTTPMiddleware`` that rejects any request whose body exceeds
``settings.max_request_body_bytes`` (config, default 25 MiB) with a Contract-2
**413 PAYLOAD_TOO_LARGE** envelope BEFORE the route handler (and therefore before the
JSON body is parsed into a model / a base64 image is decoded into memory).

Two enforcement points:

* **Content-Length** — when the client sends it, reject up front without reading a byte.
  This is the enforcement point. (An earlier version also wrapped the ASGI receive channel
  to cap chunked/no-Content-Length bodies, but reassigning ``request._receive`` inside a
  ``BaseHTTPMiddleware`` corrupts downstream body parsing — ``call_next`` streams the body
  through its own machinery — so that approach is removed. Per-handler caps (embeddings
  payload bytes, chat image bytes/count) re-validate after parsing as defence in depth, and
  uvicorn enforces its own request-size ceiling for the chunked-and-lying case.)

The middleware emits the SAME Contract-2 error envelope as the exception handlers
(code ``PAYLOAD_TOO_LARGE``) directly, because a middleware runs OUTSIDE the FastAPI
exception-handler stack for these ASGI-level rejections.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import metrics, trace

logger = structlog.get_logger(__name__)

# Contract-2 error code for an over-cap body. Distinct from VALIDATION_ERROR so clients
# can branch on it; rendered with the same envelope as the exception handlers.
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

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        limit = self._max_bytes

        # Trust a present, parseable Content-Length and reject up front (no body read). This
        # is the sole enforcement point — wrapping receive here breaks downstream parsing.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > limit:
                metrics.payload_too_large_total.labels("body_bytes").inc()
                logger.info("body_too_large", source="content_length", bytes=declared, max_bytes=limit)
                return _too_large_response(limit, declared)

        return await call_next(request)
