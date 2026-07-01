"""FastAPI application factory + lifespan wiring.

Installs the trace middleware and Contract 2 exception handlers, mounts the task /
agents / capabilities / health routers, and manages the lifespan: open the DB pool,
start the aiokafka producer + outbox publisher task, build the shared service-token
provider + downstream clients, warm JWKS — closing all on shutdown. Configures
structlog at import time.

The api routers (``api/tasks.py``, ``api/agents.py``, ``api/capabilities.py``,
``api/health.py``) are authored by the API feature agent. main.py imports them
directly — they are a hard dependency of the app, not optional.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time (before uvicorn creates the loop) fixes local
# Windows dev; it is a no-op on Linux/macOS (prod), so it is safe to set unconditionally.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import agents, capabilities, health, tasks
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.pipeline import apply_stage_flags
from .core.stages import deps as stage_deps
from .core.trace import TraceContextMiddleware, init_tracing, shutdown_tracing
from .db import pool as db_pool
from .db.outbox import OutboxPublisher
from .services.guardrails_client import GuardrailsClient
from .services.hil_client import HilClient
from .services.llms_client import LlmsClient
from .services.mcp_client import McpClient
from .services.memory_client import MemoryClient
from .services.rag_client import RagClient
from .services.registry_client import RegistryClient
from .services.service_token import ServiceTokenProvider
from .services.skill_registry_client import SkillRegistryClient
from .services.sweeper import TaskSweeper
from .services.valkey import ValkeyClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # ── OpenTelemetry span export (OPT-IN; NO-OP unless OTEL endpoint set + SDK present) ──
    # W3C trace-context propagation (traceparent + tracestate) is always on (core.trace);
    # span EXPORT is wired only when an OTLP endpoint is configured, so local/test boots
    # need no collector and incur no overhead.
    init_tracing(settings)

    # ── Stage-enable flags (STAGE_ENABLE_<NAME> env) applied to the registry ───
    # Consulted once at startup so future stages enable per-environment without
    # code edits (the registry keeps the pipeline shape; flags only flip enabled).
    apply_stage_flags(settings)

    # ── DB pool (best-effort open; readiness reflects actual connectivity) ─────
    pool = db_pool.create_pool(settings.database_url)
    app.state.db_pool = pool
    # Gated by DB_POOL_OPEN_AT_STARTUP (default ON). Opening spawns a background worker
    # that holds a libpq socket; under test that worker's C-level socket op does not yield
    # to asyncio cancellation, so a function-scoped event-loop teardown wedges in
    # _cancel_all_tasks. Tests turn this OFF (db_pool is nulled by the fixture anyway).
    if settings.db_pool_open_at_startup:
        try:
            await pool.open(wait=False)
        except Exception as exc:  # noqa: BLE001 — DB may be down at boot; readyz reports it
            logger.warning("db_pool_open_failed", error=str(exc))

    # ── Service-token provider + downstream clients (shared, connection-pooled) ──
    token_provider = ServiceTokenProvider(settings)
    app.state.token_provider = token_provider
    app.state.guardrails_client = GuardrailsClient(settings, token_provider)
    app.state.llms_client = LlmsClient(settings, token_provider)
    # Bind the SAME client instances into the stages' dependency holder. The foundation
    # pipeline runner constructs stages with no args (Pipeline.from_registry -> spec.stage_cls()),
    # so PRE/POST_GUARDRAIL + LLM stages resolve their clients via stages.deps, not __init__.
    # Without this the stages raise SERVICE_UNAVAILABLE ("... client is not configured").
    stage_deps.set_clients(
        guardrails_client=app.state.guardrails_client,
        llms_client=app.state.llms_client,
    )

    # ── WP12 enhancement-stage clients (RAG / Memory / Tool-Registry / MCP) ──────
    # Constructed here so they share the one service-token provider + a per-service
    # connection pool. Bound into the stage deps holder; the enhancement stages
    # (default-disabled) resolve them lazily only when a stage actually runs, so a
    # deployment that never enables them pays only the cheap idle httpx-client cost.
    app.state.rag_client = RagClient(settings, token_provider)
    app.state.memory_client = MemoryClient(settings, token_provider)
    app.state.registry_client = RegistryClient(settings, token_provider)
    app.state.mcp_client = McpClient(settings, token_provider)
    app.state.skill_registry_client = SkillRegistryClient(settings, token_provider)
    stage_deps.set_enhancement_clients(
        rag_client=app.state.rag_client,
        memory_client=app.state.memory_client,
        registry_client=app.state.registry_client,
        mcp_client=app.state.mcp_client,
        skill_registry_client=app.state.skill_registry_client,
    )

    # ── Human-in-the-loop client (Phase 6) — gates ask-mode tools. OFF => ask denied. ──
    if settings.hil_enabled:
        app.state.hil_client = HilClient(settings)
        stage_deps.set_hil_client(app.state.hil_client)
    else:
        app.state.hil_client = None

    # ── Valkey (SOFT dependency — lazy client; /readyz soft-reports it) ─────────
    app.state.valkey = ValkeyClient(settings.valkey_url)
    # Share the SAME Valkey handle with the LOAD-stage agent-config read-through cache.
    # SOFT: the cache fails open to a DB read when Valkey is absent/erroring; under test
    # the conftest swaps app.state.valkey for a network-free double, but stages resolve
    # the handle via stage_deps (set below), and the cache bypasses cleanly without it.
    stage_deps.set_valkey(app.state.valkey)

    # ── Outbox publisher (Kafka connect is lazy + fail-soft) ────────────────────
    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    # Gated by OUTBOX_PUBLISHER_ENABLED (default ON). OFF leaves events durable in
    # xagent.outbox without an in-process drainer — used by the test-suite to avoid a
    # real aiokafka producer (whose connect/teardown can wedge across many short loops).
    if settings.outbox_publisher_enabled:
        await publisher.start()

    # ── Backup task sweeper (WP08 — lifespan-scheduled backstop + retention) ────
    # Finalises tasks that a crashed worker left non-terminal past their deadline AND
    # runs outbox/task_steps retention. Fail-soft; a missing pool is a quiet no-op.
    # Gated by SWEEPER_ENABLED (tests leave it OFF — no DB / no background loop).
    sweeper = TaskSweeper(pool, settings)
    app.state.task_sweeper = sweeper
    if settings.sweeper_enabled:
        await sweeper.start()

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    logger.info("startup_complete", environment=settings.environment)
    try:
        yield
    finally:
        await shutdown_tracing()
        # Release the lifespan-scoped Valkey handle from the stage deps holder so the
        # module global can never leak a (now-closed) client into a later direct-stage
        # unit test — the agent-config cache then bypasses cleanly to a DB read.
        stage_deps.set_valkey(None)
        # Release the WP12 enhancement clients from the deps holder so a (now-closed)
        # client can never leak into a later direct-stage unit test.
        stage_deps.set_enhancement_clients()
        stage_deps.set_hil_client(None)
        await sweeper.stop()
        await publisher.stop()
        await app.state.valkey.aclose()
        await app.state.guardrails_client.aclose()
        await app.state.llms_client.aclose()
        await app.state.rag_client.aclose()
        await app.state.memory_client.aclose()
        await app.state.registry_client.aclose()
        await app.state.mcp_client.aclose()
        await app.state.skill_registry_client.aclose()
        if app.state.hil_client is not None:
            await app.state.hil_client.aclose()
        await token_provider.aclose()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX xAgent — agent-runtime",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(agents.router)
    app.include_router(capabilities.router)
    return app


app = create_app()
