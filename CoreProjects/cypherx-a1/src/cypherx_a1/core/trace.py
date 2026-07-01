"""Trace-context ASGI middleware + W3C propagation + OTel export (Contracts 6 & 8).

Parses ``traceparent`` + ``tracestate`` (W3C trace context), ``X-Request-ID``,
``X-Tenant-ID`` and ``X-Agent-ID`` from the inbound request, binds them into structlog
contextvars (so every log line carries the correlation fields) and stores them on
contextvars for the rest of the request lifecycle. ``propagation_headers()`` rebuilds the
downstream header set so every SharedCore call carries the SAME distributed trace.

OTel span EXPORT is OPT-IN and a NO-OP unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set AND
the OTel SDK is installed; W3C header propagation is independent of the SDK. Mirrors the
xAgent ax-1 trace module verbatim in behaviour.
"""

from __future__ import annotations

import contextvars
import re
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import metrics

if TYPE_CHECKING:
    from .config import Settings

logger = structlog.get_logger(__name__)

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")
tracestate_var: contextvars.ContextVar[str] = contextvars.ContextVar("tracestate", default="")
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)
agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_id", default=None)

_TRACESTATE_MAX_MEMBERS = 32
_TRACESTATE_MEMBER_MAX_LEN = 256
_TRACESTATE_MEMBER_RE = re.compile(r"^[^,=\s]+=[^,=]+$")


def _hex_to_uuid(hex32: str) -> str:
    return str(uuid.UUID(hex=hex32))


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """Parse a W3C ``traceparent`` into (trace_id_uuid, span_id_hex); None if malformed."""
    if not value:
        return None
    parts = value.strip().split("-")
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


def sanitize_tracestate(value: str | None) -> str:
    """Validate + normalise an inbound ``tracestate`` to a safe forwardable string."""
    if not value:
        return ""
    members: list[str] = []
    for raw in value.split(","):
        member = raw.strip()
        if not member or len(member) > _TRACESTATE_MEMBER_MAX_LEN:
            continue
        if _TRACESTATE_MEMBER_RE.match(member):
            members.append(member)
        if len(members) >= _TRACESTATE_MAX_MEMBERS:
            break
    return ",".join(members)


def current_traceparent() -> str:
    """Rebuild a W3C ``traceparent`` from the bound trace_id + span_id (fresh if unbound)."""
    trace_id = trace_id_var.get() or str(uuid.uuid4())
    trace_hex = uuid.UUID(trace_id).hex
    span_hex = span_id_var.get() or uuid.uuid4().hex[:16]
    return f"00-{trace_hex}-{span_hex}-01"


def current_tracestate() -> str:
    return tracestate_var.get()


def propagation_headers() -> dict[str, str]:
    """Build the W3C trace-context header set for a downstream call."""
    headers = {
        "traceparent": current_traceparent(),
        "X-Request-ID": request_id_var.get(),
    }
    state = current_tracestate()
    if state:
        headers["tracestate"] = state
    return headers


# ── OpenTelemetry span export (opt-in; NO-OP unless endpoint set + SDK installed) ──
_tracer_provider: Any | None = None


def init_tracing(settings: Settings) -> None:
    """Wire the OTLP span exporter IFF the endpoint is set and the OTel SDK is installed."""
    global _tracer_provider
    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if not endpoint:
        metrics.otel_tracing_enabled.set(0)
        logger.info("otel_tracing_disabled", reason="no_endpoint")
        return
    if _tracer_provider is not None:
        return

    try:
        provider = _build_tracer_provider(endpoint, settings)
    except Exception as exc:  # noqa: BLE001 — a tracing-export failure must never fail boot
        metrics.otel_tracing_enabled.set(0)
        logger.warning("otel_tracing_init_failed", endpoint=endpoint, error=str(exc))
        return

    _tracer_provider = provider
    metrics.otel_tracing_enabled.set(1)
    logger.info("otel_tracing_enabled", endpoint=endpoint, service_name=settings.otel_service_name)


def _build_tracer_provider(endpoint: str, settings: Settings) -> Any:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    protocol = (settings.otel_exporter_otlp_protocol or "grpc").lower()
    if protocol in ("http", "http/protobuf"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    otel_trace.set_tracer_provider(provider)
    return provider


async def shutdown_tracing() -> None:
    global _tracer_provider
    if _tracer_provider is None:
        return
    try:
        _tracer_provider.shutdown()
    except Exception as exc:  # noqa: BLE001 — shutdown must never raise
        logger.warning("otel_tracing_shutdown_failed", error=str(exc))
    finally:
        _tracer_provider = None


class TraceContextMiddleware:
    """Pure-ASGI middleware that binds correlation context for the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        request_id = headers.get("x-request-id")
        if not request_id:
            request_id = str(uuid.uuid4())
            logger.warning("request_id_generated_fallback", request_id_generated_fallback=True)

        parsed = parse_traceparent(headers.get("traceparent"))
        if parsed is None:
            trace_id = str(uuid.uuid4())
            span_id = uuid.uuid4().hex[:16]
        else:
            trace_id, span_id = parsed

        tracestate = sanitize_tracestate(headers.get("tracestate"))
        tenant_id = headers.get("x-tenant-id")
        agent_id = headers.get("x-agent-id")

        request_id_var.set(request_id)
        trace_id_var.set(trace_id)
        span_id_var.set(span_id)
        tracestate_var.set(tracestate)
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
