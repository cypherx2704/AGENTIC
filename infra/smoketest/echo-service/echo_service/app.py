"""echo-service — CypherX Phase 1 infra smoke-test app (Component 21).

Endpoints:
  GET /echo     -> request headers as JSON + ONE Contract 6 log line (assertion 1/2)
  GET /livez    -> Contract 7 liveness (NEVER checks downstreams)
  GET /readyz   -> Contract 7 readiness (DB + Valkey health gate)
  GET /metrics  -> Prometheus exposition on :9090 (assertion 6)

Startup:
  * init OTEL tracing (Contract 8, W3C trace context -> Tempo OTLP gRPC)
  * run the PgBouncer RLS probe + Valkey PING (sets initial readiness)
  * produce ONE Contract 5 envelope to cypherx.smoketest.event (assertion 5)
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from . import __version__
from .config import settings
from .deps import pg_rls_probe, produce_startup_event, valkey_ping
from .logging_setup import log
from .tracing import current_trace_id, current_traceparent, init_tracing

_START = time.monotonic()

# ---- Prometheus metrics (Contract 7 /metrics). Job label set by ServiceMonitor;
# Prometheus exposes these so `up{job="echo"} == 1` (assertion 6) holds. ----
_REGISTRY = CollectorRegistry()
_HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled by echo-service.",
    ["method", "path", "status"],
    registry=_REGISTRY,
)
_HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    registry=_REGISTRY,
)


# Readiness state, refreshed by the startup probes and by /readyz on demand.
class _State:
    db_ok: bool = False
    kafka_ok: bool = False
    valkey_ok: bool = False


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log(
        "INFO",
        "echo-service starting",
        version=__version__,
        environment=settings.environment,
    )
    init_tracing(app)

    # A single trace_id correlates the startup event with the produced envelope.
    startup_trace_id = current_trace_id() or uuid.uuid4().hex

    # Dependency probes — readiness gate (Contract 7 / Component 21).
    state.db_ok = await pg_rls_probe()
    state.valkey_ok = await valkey_ping()
    # Produce exactly one Contract 5 event on startup (assertion 5).
    state.kafka_ok = await produce_startup_event(startup_trace_id)

    if not (state.db_ok and state.valkey_ok):
        # We do NOT exit — readiness stays 503 so the pod is pulled from the LB
        # until deps recover (Contract 7). Liveness remains green.
        log(
            "WARN",
            "echo-service started with unhealthy downstreams",
            extra_db_ok=state.db_ok,
            extra_valkey_ok=state.valkey_ok,
        )
    else:
        log("INFO", "echo-service ready")

    yield
    log("INFO", "echo-service shutting down")


app = FastAPI(
    title="echo-service",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


def _uptime() -> int:
    return int(time.monotonic() - _START)


@app.get("/echo")
async def echo(request: Request) -> JSONResponse:
    """Return request headers as JSON and emit ONE Contract 6 log line.

    Also surfaces SMOKE_SECRET_LEN (assertion 9 — Doppler-synced secret) and the
    current `traceparent` (assertion 1 — populated by Istio at the mesh edge).
    """
    start = time.perf_counter()
    headers = {k.lower(): v for k, v in request.headers.items()}
    traceparent = headers.get("traceparent") or current_traceparent()
    request_id = headers.get("x-request-id") or str(uuid.uuid4())
    tenant_id = headers.get("x-tenant-id") or settings.fake_tenant_id
    trace_id = current_trace_id()

    body = {
        "service": settings.service,
        "version": __version__,
        "environment": settings.environment,
        "method": request.method,
        "path": request.url.path,
        "headers": headers,
        "traceparent": traceparent,
        # Echo only the LENGTH of the Doppler-synced secret — never the value.
        "smoke_secret_len": len(settings.smoke_secret),
        "SMOKE_SECRET_LEN": len(settings.smoke_secret),
    }

    duration_ms = int((time.perf_counter() - start) * 1000)
    # The ONE Contract 6 log line per /echo call (assertion 2).
    log(
        "INFO",
        "echo request handled",
        trace_id=trace_id,
        request_id=request_id,
        tenant_id=tenant_id,
        duration_ms=duration_ms,
        endpoint="/echo",
    )

    _HTTP_REQUESTS.labels("GET", "/echo", "200").inc()
    _HTTP_LATENCY.labels("GET", "/echo").observe(time.perf_counter() - start)
    resp = JSONResponse(body)
    if traceparent:
        # Forward the W3C header on the response so the script can pull trace_id
        # from it (assertion 4: Tempo lookup by trace_id).
        resp.headers["traceparent"] = traceparent
    resp.headers["x-request-id"] = request_id
    return resp


@app.get("/livez")
async def livez() -> JSONResponse:
    """Contract 7 liveness. NEVER checks downstreams."""
    _HTTP_REQUESTS.labels("GET", "/livez", "200").inc()
    return JSONResponse(
        {"status": "ok", "version": __version__, "uptime_seconds": _uptime()}
    )


@app.get("/readyz")
async def readyz() -> Response:
    """Contract 7 readiness. 503 if Postgres OR Valkey is unhealthy."""
    # Re-probe on each readiness check so a recovered downstream re-adds the pod.
    state.db_ok = await pg_rls_probe()
    state.valkey_ok = await valkey_ping()
    checks = {
        "database": "ok" if state.db_ok else "failed",
        "valkey": "ok" if state.valkey_ok else "failed",
        # kafka is a startup-only produce; report last known result, not a gate
        # for serving traffic (the produce already happened once at startup).
        "kafka": "ok" if state.kafka_ok else "failed",
    }
    ready = state.db_ok and state.valkey_ok
    status_code = 200 if ready else 503
    _HTTP_REQUESTS.labels("GET", "/readyz", str(status_code)).inc()
    return JSONResponse({"ready": ready, "checks": checks}, status_code=status_code)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus exposition (Contract 7). Scraped on :9090 by the ServiceMonitor."""
    data = generate_latest(_REGISTRY)
    return PlainTextResponse(data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)
