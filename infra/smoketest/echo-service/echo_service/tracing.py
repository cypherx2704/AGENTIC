"""OpenTelemetry tracing setup (Contract 8).

Istio injects/propagates `traceparent` at the mesh layer; the application SDK
emits OTLP spans to the SAME Tempo OTLP gRPC endpoint
(tempo-distributor.observability.svc.cluster.local:4317, Component 13) so
service-level spans join the Kong->echo trace. Propagation is W3C Trace Context
only (Contract 8 — tracecontext,baggage; never b3/jaeger).

Wrapped in best-effort try/except: a missing collector must never crash the
smoke-test pod (liveness independence, Contract 7).
"""

from __future__ import annotations

from .config import settings
from .logging_setup import log

_tracer = None


def init_tracing(app) -> None:
    """Initialise the global tracer provider and instrument FastAPI."""
    global _tracer
    if not settings.otel_enabled:
        log("INFO", "OTEL disabled — skipping tracing init")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        resource = Resource.create(
            {
                "service.name": settings.service,
                "service.version": settings.version,
                "deployment.environment": settings.environment,
            }
        )
        provider = TracerProvider(resource=resource)
        # OTLP gRPC -> Tempo distributor (Component 13).
        exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # W3C Trace Context propagation ONLY (Contract 8).
        set_global_textmap(TraceContextTextMapPropagator())

        FastAPIInstrumentor.instrument_app(app)
        _tracer = trace.get_tracer(settings.service)
        log("INFO", "OTEL tracing initialised", otel_endpoint=settings.otel_endpoint)
    except Exception as exc:  # noqa: BLE001 — tracing must never crash the pod
        log("WARN", "OTEL tracing init failed (continuing without it)", error=str(exc))


def current_traceparent() -> str | None:
    """Return the current span as a W3C `traceparent` header value, if any."""
    try:
        from opentelemetry import trace
        from opentelemetry.trace import format_span_id, format_trace_id

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return None
        flags = "01" if ctx.trace_flags.sampled else "00"
        return (
            f"00-{format_trace_id(ctx.trace_id)}-"
            f"{format_span_id(ctx.span_id)}-{flags}"
        )
    except Exception:  # noqa: BLE001
        return None


def current_trace_id() -> str | None:
    try:
        from opentelemetry import trace
        from opentelemetry.trace import format_trace_id

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format_trace_id(ctx.trace_id)
        return None
    except Exception:  # noqa: BLE001
        return None
