"""W3C trace-context middleware + propagation (Contracts 6 & 8). Compact, no OTel export."""

from __future__ import annotations

import contextvars
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")
tracestate_var: contextvars.ContextVar[str] = contextvars.ContextVar("tracestate", default="")


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    _v, trace_hex, span_hex, _f = parts
    if len(trace_hex) != 32 or len(span_hex) != 16 or trace_hex == "0" * 32 or span_hex == "0" * 16:
        return None
    try:
        return str(uuid.UUID(hex=trace_hex)), span_hex
    except ValueError:
        return None


def current_traceparent() -> str:
    trace_id = trace_id_var.get() or str(uuid.uuid4())
    trace_hex = uuid.UUID(trace_id).hex
    span_hex = span_id_var.get() or uuid.uuid4().hex[:16]
    return f"00-{trace_hex}-{span_hex}-01"


def propagation_headers() -> dict[str, str]:
    headers = {"traceparent": current_traceparent(), "X-Request-ID": request_id_var.get()}
    state = tracestate_var.get()
    if state:
        headers["tracestate"] = state
    return headers


class TraceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        parsed = parse_traceparent(headers.get("traceparent"))
        if parsed is None:
            trace_id, span_id = str(uuid.uuid4()), uuid.uuid4().hex[:16]
        else:
            trace_id, span_id = parsed
        request_id_var.set(request_id)
        trace_id_var.set(trace_id)
        span_id_var.set(span_id)
        tracestate_var.set(headers.get("tracestate", ""))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id, span_id=span_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()
