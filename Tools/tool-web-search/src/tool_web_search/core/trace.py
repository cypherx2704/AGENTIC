"""Trace-context ASGI middleware (Contracts 6 & 8).

Parses ``traceparent`` (W3C trace context), ``X-Request-ID``, ``X-Tenant-ID`` and
``X-Agent-ID`` from the inbound request and binds them into structlog's
contextvars so every log line emitted while handling the request carries the
correlation fields. Also exposes the parsed values on ``request.state`` for the
handlers and stores the trace id / request id on contextvars for the rest of the
request lifecycle (usage-record provenance).

Per Component 4 provenance rules:
  * ``request_id`` = inbound ``X-Request-ID``; synthesised (UUIDv4) + WARN if absent.
  * ``trace_id``   = 16-byte trace id parsed from ``traceparent``; synthesised + WARN
    if missing. Stored as a UUID (same 128-bit width).
"""

from __future__ import annotations

import contextvars
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

# Request-scoped correlation values, readable anywhere downstream of the middleware.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)
agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_id", default=None)


def _hex_to_uuid(hex32: str) -> str:
    """Render a 32-char hex trace id as a canonical UUID string (same 128-bit width)."""
    return str(uuid.UUID(hex=hex32))


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` header into (trace_id_uuid, span_id_hex).

    Returns ``None`` if the header is missing or malformed so the caller can
    synthesise a fresh trace.
    """
    if not value:
        return None
    parts = value.strip().split("-")
    # version-traceid(32hex)-spanid(16hex)-flags
    if len(parts) != 4:
        return None
    _version, trace_hex, span_hex, _flags = parts
    if len(trace_hex) != 32 or len(span_hex) != 16:
        return None
    if trace_hex == "0" * 32 or span_hex == "0" * 16:
        return None
    try:
        return _hex_to_uuid(trace_hex), span_hex
    except ValueError:
        return None


class TraceContextMiddleware:
    """Pure-ASGI middleware that binds correlation context for the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }

        # ── request_id ────────────────────────────────────────────────────────
        request_id = headers.get("x-request-id")
        if not request_id:
            request_id = str(uuid.uuid4())
            logger.warning("request_id_generated_fallback", request_id_generated_fallback=True)

        # ── trace_id / span_id ──────────────────────────────────────────────────
        parsed = parse_traceparent(headers.get("traceparent"))
        if parsed is None:
            trace_id = str(uuid.uuid4())
            span_id = uuid.uuid4().hex[:16]
            logger.warning("traceparent_synthesised", traceparent_synthesised=True)
        else:
            trace_id, span_id = parsed

        tenant_id = headers.get("x-tenant-id")
        agent_id = headers.get("x-agent-id")

        # Bind onto contextvars for the rest of the request lifecycle.
        request_id_var.set(request_id)
        trace_id_var.set(trace_id)
        span_id_var.set(span_id)
        tenant_id_var.set(tenant_id)
        agent_id_var.set(agent_id)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                hdrs = message.setdefault("headers", [])
                hdrs.append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()
