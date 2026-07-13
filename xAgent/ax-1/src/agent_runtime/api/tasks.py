"""Task endpoints (Component 2) — the public submit/get critical path.

  * ``POST /v1/tasks``            — submit a task (mode=sync only, first cycle) and run
    the staged execution pipeline synchronously, returning the Contract 3 response.
  * ``GET  /v1/tasks/{task_id}``  — fetch a task result (RLS-scoped) and rebuild the
    same Contract 3 response from the persisted task row + audit steps.

How this layer drives the pipeline (the concrete stages — LOAD / PRE_GUARDRAIL /
PROMPT_BUILD / LLM / POST_GUARDRAIL — are authored + bound by the stages feature agent
via ``core.pipeline.bind_stage``; this layer never implements them):

  1. ``require_principal`` verifies the inbound agent JWT and yields identity. Identity
     (tenant_id / agent_id) is read ONLY from the JWT, never from the body (Contract 13).
  2. ``TaskRequest`` validates the body (mode!=sync / bad timeout / over-cap input /
     reserved keys -> 422 VALIDATION_ERROR). The caller-vs-target rule (amended fix #6)
     is enforced before any load/persistence: ``body.agent_id`` must equal the JWT's
     ``agent_id`` (api_key-only tokens carry no agent identity) -> 422 VALIDATION_ERROR.
  3. ``create_task`` inserts the ``pending`` row; ``mark_running`` flips it to ``running``.
  4. A ``PipelineContext`` carries the principal, forwarded agent JWT, trace ids, the
     task row, a fresh ``StepBuffer``, the pool, and the wall-clock start.
  5. The :class:`EventStage` (constructed here, supplied to ``Pipeline.from_registry``)
     persists the audit steps + finalises the task and emits the Kafka event atomically
     via ``outbox.record_task_event``. It runs ALWAYS (success AND short-circuit).
  6. The Contract 3 response is assembled from ``ctx.steps.steps`` (the SAME ordered
     list that was persisted) via ``a2a.build_task_response`` + ``a2a.build_step``
     (FIX 2 redacted->passed; FIX 3 schema_version + started_at + cost_usd + task_steps
     always present). A ``GUARDRAIL_VIOLATION`` terminal error renders as HTTP 422 with
     the Contract 2 envelope; all other terminal errors render as a Contract 3 response
     with a non-completed status + populated ``error`` field.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Importing core.stages runs register_stages() which binds the concrete LOAD/PRE_GUARDRAIL/
# PROMPT_BUILD/LLM/POST_GUARDRAIL classes into STAGE_REGISTRY (without it from_registry runs ONLY
# EVENT). It is also the home of the canonical EventStage, constructed in submit_task below.
from ..core import metrics, trace
from ..core import stages as _stages
from ..core.auth import Principal, require_principal
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode
from ..core.pipeline import Pipeline, PipelineContext
from ..core.validation import parse_rfc3339_query, parse_uuid_path, parse_uuid_query
from ..db import steps_repo, tasks_repo
from ..db.steps_repo import StepBuffer, StepRow
from ..db.tasks_repo import TaskRow
from ..models import a2a
from ..models.task import TaskRequest
from ..services.auth_client import AuthClient

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["tasks"])

# Idempotency-Key header (Contract 9). Optional — absence disables idempotency for that
# request (a client that wants exactly-once retry sends a stable key per logical request).
_IDEMPOTENCY_HEADER = "Idempotency-Key"

# GET /v1/tasks list paging bounds (page-size guardrails for the Task Feed).
_LIST_DEFAULT_PAGE_SIZE = 50
_LIST_MAX_PAGE_SIZE = 200
# The list statuses a client may filter by (the full task status enum).
_LIST_FILTERABLE_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "cancelled", "timeout"}
)


def _valkey_client(request: Request) -> Any | None:
    """Return the CONFIGURED Valkey client, or None.

    A real ``services.valkey.ValkeyClient`` exposes the WP08 helper methods
    (``set_cancel_signal`` etc.). The conftest swaps in a network-free double that does
    NOT — so we treat "no helper methods" as "Valkey NOT configured" (the unit case):
    idempotency is then disabled/allow, cancel has no store, authorize fails open. Only a
    real, configured-but-erroring client takes the fail-closed 503 / 503-cancel paths.
    """
    valkey = getattr(request.app.state, "valkey", None)
    if valkey is None or not hasattr(valkey, "set_cancel_signal"):
        return None
    return valkey


def _auth_client(request: Request) -> AuthClient:
    """Return the shared AuthClient, building one lazily from app.state if absent.

    Mirrors ``api/agents.py``: reuse a wired ``app.state.auth_client`` when present, else
    construct one over the shared service-token provider so identity headers stay
    consistent. Cached on app.state for reuse.
    """
    existing = getattr(request.app.state, "auth_client", None)
    if existing is not None:
        return existing
    settings = getattr(request.app.state, "settings", None) or get_settings()
    token_provider = request.app.state.token_provider
    client = AuthClient(settings, token_provider)
    request.app.state.auth_client = client
    return client


# Internal step status -> Contract 2 task status family. GUARDRAIL_VIOLATION is the only
# terminal error rendered as an HTTP error envelope (422); the rest are carried inside a
# Contract 3 response with a non-completed status.
_GUARDRAIL_CODE = ErrorCode.GUARDRAIL_VIOLATION


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The EVENT stage (the finally-equivalent terminal stage) is the single authoritative
# implementation in ``core/stages/event.py``; submit_task constructs one instance below.


def _terminal_status(ctx: PipelineContext) -> str:
    """Map ``ctx.terminal_error`` to the terminal task status (default 'completed')."""
    if ctx.terminal_error is None:
        return "completed"
    # TerminalError.status is one of failed | timeout | cancelled.
    return ctx.terminal_error.status or "failed"


# ── POST /v1/tasks ────────────────────────────────────────────────────────────────
@router.post("/tasks", response_model=None)
async def submit_task(
    body: TaskRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
    mode: str = Query(
        default="sync",
        description="Execution mode: 'sync' (default, runs inline + returns the terminal "
        "Contract-3 result) or 'async' (runs in the background + returns 202 with the "
        "task_id; poll GET /v1/tasks/{id}). Idempotency-Key is REQUIRED for async.",
    ),
) -> JSONResponse:
    """Submit a task; run it inline (sync) or in the background (async, WP12).

    WP08 reliability flow (unchanged for both modes), in order:
      1. caller-vs-target rule;
      2. Auth layer-B ``task:execute`` authorize (Valkey-cached verdict, fail-open);
      3. Contract-9 idempotency reservation (FAIL-CLOSED when a CONFIGURED Valkey errors;
         disabled/allow when no Valkey is configured — the unit case);
      4. run the pipeline under a per-task ``asyncio.timeout`` + a cooperative cancel poller.

    WP12 async mode (``?mode=async``): the pipeline runs in a fire-and-forget background
    task and the endpoint returns **202 Accepted** immediately with the task_id (status
    ``running``). Polling is via ``GET /v1/tasks/{id}``; live progress via the SSE stream.
    Idempotency-Key is REQUIRED for async (a crashed-worker retry must be safe) — a missing
    key is a 422. Crash recovery: a background task that dies leaves a non-terminal row that
    the WP08 sweeper finalises (failed) once it passes its deadline.
    """
    settings = get_settings()
    is_async = _resolve_async_mode(mode, settings)

    # 1) Caller-vs-target rule (amended fix #6 — FIRST-CYCLE RULE, checked BEFORE any agent
    # load or persistence): the submitting agent may invoke ONLY its own runtime —
    # body.agent_id MUST equal the verified JWT's agent_id. Cross-agent invocation
    # arrives only via 9B A2A delegation tokens (📋). Without this check, task rows /
    # Kafka events / cost would be attributed to an agent whose config never ran.
    if principal.agent_id is None:
        # api_key-only callers carry no agent identity; the target cannot be authorized.
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "token carries no agent identity; agent_id cannot be validated against the caller.",
            details={"reason": "NO_AGENT_IDENTITY"},
        )
    if body.agent_id != principal.agent_id:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "agent_id must equal the authenticated agent's id; "
            "cross-agent invocation requires an A2A delegation token.",
            details={"reason": "AGENT_ID_MISMATCH", "agent_id": body.agent_id},
        )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Task store is not available.")

    valkey = _valkey_client(request)

    # Idempotency-Key is REQUIRED for async (so a crashed-worker retry is exactly-once
    # safe): we return 202 before the run completes, so the ONLY guard against a duplicate
    # background run is the reservation. A missing key on async is a hard 422 — checked
    # BEFORE persistence so a bad async submit never creates an orphan row.
    idem_key = request.headers.get(_IDEMPOTENCY_HEADER)
    if is_async and not idem_key:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Idempotency-Key header is REQUIRED for mode=async.",
            details={"reason": "ASYNC_REQUIRES_IDEMPOTENCY_KEY"},
        )

    # 2) Auth layer-B authorize (task:execute) — Valkey-cached verdict, FAIL-OPEN. A
    # definitive Auth deny raises FORBIDDEN (403); an Auth/Valkey error accepts (the JWT
    # was already verified upstream; the 60s cache keeps revocation prompt).
    await _authorize_submit(request, principal, settings, valkey)

    # 3) Idempotency reservation (Contract 9). Returns a "finish" callback that stores the
    # response for replay (or releases the reservation on failure); raises 409 on a
    # duplicate in_flight, replays a stored completed response, and 503s FAIL-CLOSED when a
    # CONFIGURED Valkey errors. No Valkey configured (unit) -> disabled (no-op finish).
    idem_fingerprint = _request_fingerprint(body) if idem_key else None
    replay, finish_idem = await _idempotency_begin(
        principal, settings, valkey, idem_key, idem_fingerprint
    )
    if replay is not None:
        return replay

    trace_id = trace.trace_id_var.get()
    request_id = trace.request_id_var.get()

    # Create the pending row, then flip to running. Identity (tenant_id) is from the JWT;
    # body.agent_id equals the JWT agent (enforced above) — the LOAD stage resolves its config.
    # session_id (Contract-3 input field, WP12) is persisted on the row (column added by the
    # stage agent's migration; the repo write-through is already wired). cost_budget_per_task
    # is likewise threaded when the caller supplied it.
    session_id = getattr(body, "session_id", None)
    task: TaskRow = await tasks_repo.create_task(
        pool,
        tenant_id=principal.tenant_id,
        agent_id=body.agent_id,
        trace_id=trace_id,
        task_input=body.input.model_dump(),
        timeout_seconds=body.timeout_seconds,
        user_id=_user_id_from_claims(principal),
        metadata=body.metadata,
        session_id=session_id,
        cost_budget_per_task=getattr(body, "cost_budget_per_task", None),
    )
    await tasks_repo.mark_running(pool, principal.tenant_id, task.task_id)

    # Register the session idempotently (a session-per-principal correlation, WP12). The
    # durable record is the persisted tasks.session_id above; this additionally registers
    # the session with the sessions/memory concept when wired. Fail-soft — never blocks.
    if session_id:
        await _register_session(request, principal, session_id)

    ctx = PipelineContext(
        principal=principal,
        inbound_agent_jwt=principal.raw_token,
        trace_id=trace_id,
        request_id=request_id,
        task=task,
        steps=StepBuffer(),
        pool=pool,
        started_monotonic=time.monotonic(),
        started_at=tasks_repo.now_iso(),
        cancel_check=_make_cancel_check(valkey, settings, principal.tenant_id, task.task_id),
    )
    # Attach a per-task SSE event publisher to the context (WP12). The pipeline / stages
    # MAY call ``ctx.publish_event(...)`` to push live step frames to the SSE channel; it is
    # a no-op when Valkey is absent. Set as a plain attribute (PipelineContext is a vanilla
    # dataclass) so the stage-owned pipeline module need not change to carry it.
    ctx.publish_event = _make_event_publisher(  # type: ignore[attr-defined]
        valkey, settings, principal.tenant_id, task.task_id
    )

    budget = min(settings.task_timeout_seconds, body.timeout_seconds)

    if is_async:
        # ── ASYNC: fire-and-forget the run, return 202 immediately ──────────────────
        # Publish the initial 'accepted/running' frame so an SSE subscriber that connects
        # right after the 202 sees the task is live (the run starts on the next loop tick).
        await _publish_event(
            valkey, settings, principal.tenant_id, task.task_id,
            _progress_event("accepted", task_id=task.task_id, status="running"),
        )
        runner = asyncio.ensure_future(
            _run_task_in_background(ctx, settings, valkey, budget, finish_idem)
        )
        _track_background_task(request.app, runner)
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task.task_id,
                "status": "running",
                "mode": "async",
                "trace_id": trace_id,
                "message": "Task accepted; poll GET /v1/tasks/{id} or stream "
                "GET /v1/tasks/{id}/stream for progress.",
            },
        )

    # ── SYNC (default, unchanged behaviour): run inline + return the terminal result ──
    await _run_pipeline_guarded(ctx, settings, valkey, budget)
    response = _response_from_context(ctx)
    await finish_idem(response)
    return response


async def _run_pipeline_guarded(
    ctx: PipelineContext, settings: Settings, valkey: Any | None, budget: float
) -> None:
    """Run the bound pipeline under the per-task timeout + cancel poller (shared sync/async).

    Wraps ``Pipeline.run`` in ``asyncio.timeout(budget)`` exactly as the WP08 sync path did;
    a budget overrun marks the task timeout + finalises via EVENT (EVENT did not run inside
    the cancelled run). Clears the cancel flag on the way out. This is the EXACT behaviour
    the previous inline sync flow had — extracted so the async background runner reuses it.
    """
    event_stage = _stages.EventStage(producer_version=settings.service_version)
    pipeline = Pipeline.from_registry(event_stage)
    try:
        async with asyncio.timeout(budget):
            await pipeline.run(ctx)
    except TimeoutError:
        # The run overran its budget. The pipeline's stages were cancelled; mark the task
        # timeout + emit the terminal event ourselves (EVENT did not get to run inside the
        # cancelled run). The EVENT stage is idempotent enough (single UPDATE) for this.
        metrics.task_timeouts_total.inc()
        logger.warning("task_timed_out", task_id=ctx.task.task_id, budget_s=budget)
        if ctx.terminal_error is None:
            ctx.fail(ErrorCode.SERVICE_UNAVAILABLE, "Task exceeded its time budget.", status="timeout")
        await _finalise_after_timeout(ctx, settings)

    # Best-effort: clear the cancel flag now the task is terminal (TTL backstops a failure).
    await _clear_cancel(valkey, settings, ctx.task.tenant_id, ctx.task.task_id)


async def _run_task_in_background(
    ctx: PipelineContext,
    settings: Settings,
    valkey: Any | None,
    budget: float,
    finish_idem: Any,
) -> None:
    """Background driver for an async-mode task (WP12): run, finalise, publish, store.

    Mirrors the sync flow with no HTTP response on the line: run the guarded pipeline, then
    build the same Contract-3 body, publish the terminal SSE frame, and store it for
    idempotent replay (so a duplicate async retry on the SAME key replays the stored body).
    Never raises — a crash here would otherwise just leave a non-terminal row, which the
    WP08 sweeper finalises (failed) past its deadline (crash recovery).
    """
    task_id = ctx.task.task_id
    try:
        await _run_pipeline_guarded(ctx, settings, valkey, budget)
        # Build the terminal Contract-3 body (mirrors the sync return). A GUARDRAIL_VIOLATION
        # would raise ApiError in _response_from_context (a 422 envelope); for the background
        # path we render that as a Contract-3 'failed' body via the row instead of raising.
        response = _async_terminal_response(ctx)
        await _publish_event(
            valkey, settings, ctx.task.tenant_id, task_id,
            _terminal_event_from_response(task_id, response),
        )
        await finish_idem(response)
    except Exception as exc:  # noqa: BLE001 — a background run must never crash the worker
        logger.error("async_task_run_failed", task_id=task_id, error=str(exc), exc_info=exc)


# ── GET /v1/tasks/{task_id} ─────────────────────────────────────────────────────────
@router.get("/tasks/{task_id}", response_model=None)
async def get_task(
    task_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Return the Contract 3 response for a previously-submitted task (RLS-scoped)."""
    # Validate the path UUID BEFORE the repo binds it to the tasks.task_id ``uuid`` column
    # (BUG 1): a non-UUID id cannot name any row -> 404, never an uncastable-value 5xx.
    task_id = parse_uuid_path(task_id, param="Task")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Task store is not available.")

    row = await tasks_repo.get_task(pool, principal.tenant_id, task_id)
    if row is None:
        # RLS hides cross-tenant rows -> they surface as NOT_FOUND (never leak existence).
        raise ApiError(ErrorCode.NOT_FOUND, f"Task {task_id} not found.")

    steps = await steps_repo.list_steps(pool, principal.tenant_id, task_id)
    response = _response_from_task_row(row, steps)
    return JSONResponse(content=response)


# ── GET /v1/tasks/{task_id}/stream (SSE progress relay, WP12) ────────────────────────
@router.get("/tasks/{task_id}/stream", response_model=None)
async def stream_task(
    task_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    """Relay a task's step/stage progress + terminal result as Server-Sent Events.

    ``text/event-stream`` of SSE frames (``event: <type>\\n data: <json>\\n\\n``). Event
    types: ``snapshot`` (current status + task_steps), ``step`` (a single stage frame
    published live by the pipeline), ``content_filter`` (a guardrail blocked the task —
    terminal), ``done`` (the task completed — terminal), ``error`` (the task failed /
    timed out / was cancelled — terminal). After a terminal frame the stream closes.

    Transport: Valkey Pub/Sub on the per-task channel is the live path (the pipeline
    publishes step frames). FAIL-SOFT: when Pub/Sub is unavailable (Valkey absent /
    erroring) the relay FALLS BACK to polling the task row + steps and emitting periodic
    ``snapshot`` frames until the task is terminal — so SSE still works degraded with no
    infra. 404 for unknown / cross-tenant task (RLS hides it); auth required.
    """
    settings = get_settings()
    if not settings.sse_streaming_enabled:
        raise ApiError(ErrorCode.NOT_FOUND, "Task streaming is not enabled.")

    # Validate the path UUID BEFORE the repo binds it (BUG 1) — a non-UUID id is a 404.
    task_id = parse_uuid_path(task_id, param="Task")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Task store is not available.")

    # Existence + tenant scoping up front so an unknown / cross-tenant task is a clean 404
    # BEFORE we open the long-lived stream (RLS hides cross-tenant rows -> NOT_FOUND).
    row = await tasks_repo.get_task(pool, principal.tenant_id, task_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Task {task_id} not found.")

    valkey = _valkey_client(request)
    generator = _sse_event_stream(
        pool=pool,
        valkey=valkey,
        settings=settings,
        tenant_id=principal.tenant_id,
        task_id=task_id,
        initial_row=row,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering so frames flush live
        },
    )


# ── GET /v1/tasks (list — frontend Task Feed dependency) ───────────────────────────
@router.get("/tasks", response_model=None)
async def list_tasks(
    request: Request,
    principal: Principal = Depends(require_principal),
    since: str | None = Query(default=None, description="Only tasks created at/after this RFC 3339 instant."),
    status: str | None = Query(default=None, description="Filter by task status (exact)."),
    agent_id: str | None = Query(default=None, description="Filter by target agent id."),
    cursor: str | None = Query(default=None, description="Opaque pagination cursor from a prior page."),
    limit: int = Query(default=_LIST_DEFAULT_PAGE_SIZE, ge=1, le=_LIST_MAX_PAGE_SIZE),
) -> JSONResponse:
    """List the tenant's tasks newest-first with cursor pagination + filters.

    Contract: ``{ "tasks": [<redaction-safe summary>...], "next_cursor": <opaque|null> }``.
    Each summary carries ids, status, usage, timestamps, error_code, metadata — NEVER the
    free-form ``input`` / ``output`` / ``error_msg`` (redaction-safe projection). RLS scopes
    every row to the JWT tenant. ``status`` must be a known task status (else 422). ``cursor``
    is the opaque ``(created_at, task_id)`` keyset from the previous page's ``next_cursor``;
    ``next_cursor`` is null on the last page.
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Task store is not available.")

    if status is not None and status not in _LIST_FILTERABLE_STATUSES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "status must be one of the known task statuses.",
            details={"reason": "BAD_STATUS_FILTER", "allowed": sorted(_LIST_FILTERABLE_STATUSES)},
        )

    # Validate the castable query filters BEFORE the repo binds them (BUG 1): a malformed
    # ?since (timestamptz) or ?agent_id (uuid) is a 422 client error, never an uncastable
    # 5xx. The canonical/normalised value is forwarded (an already-valid value is unchanged).
    since = parse_rfc3339_query(since, param="since")
    agent_id = parse_uuid_query(agent_id, param="agent_id")

    cursor_created_at, cursor_task_id = _decode_cursor(cursor)

    # Fetch one extra row to detect whether a further page exists (then trim to `limit`).
    rows = await tasks_repo.list_tasks(
        pool,
        principal.tenant_id,
        limit=limit + 1,
        cursor_created_at=cursor_created_at,
        cursor_task_id=cursor_task_id,
        since=since,
        status=status,
        agent_id=agent_id,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if has_more and page else None

    return JSONResponse(
        content={"tasks": [_list_item_to_json(r) for r in page], "next_cursor": next_cursor}
    )


# ── DELETE /v1/tasks/{task_id} (cooperative cancel) ────────────────────────────────
@router.delete("/tasks/{task_id}", response_model=None)
async def cancel_task(
    task_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Request cooperative cancellation of a running task.

    Status matrix (decided semantics):
      * 202 Accepted — the task is non-terminal and a cancel signal was set (the pipeline
        observes it BETWEEN stages and, mid-LLM, cancels the in-flight call; EVENT then
        marks the task ``cancelled`` + emits the terminal event).
      * 409 Conflict — the task is already terminal (completed/failed/cancelled/timeout);
        there is nothing to cancel.
      * 404 Not Found — unknown task, or another tenant's task (RLS hides it; we never
        leak existence).
      * 503 Service Unavailable — the cancel-signal store (a CONFIGURED Valkey) is
        unreachable, so we cannot GUARANTEE the running task will see the signal (decided
        semantics: if Valkey is down we return 503 rather than a false 202). When no
        Valkey is configured at all (the unit case) there is no cancel store to guarantee
        against -> 503 as well (we cannot honour a cancel without a signal channel).
    """
    # Validate the path UUID BEFORE the repo binds it (BUG 1) — a non-UUID id is a 404.
    task_id = parse_uuid_path(task_id, param="Task")
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Task store is not available.")

    row = await tasks_repo.get_task(pool, principal.tenant_id, task_id)
    if row is None:
        metrics.task_cancels_total.labels("not_found").inc()
        raise ApiError(ErrorCode.NOT_FOUND, f"Task {task_id} not found.")

    if row.status in tasks_repo.TERMINAL_STATUSES:
        metrics.task_cancels_total.labels("conflict").inc()
        raise ApiError(
            ErrorCode.CONFLICT,
            f"Task {task_id} is already {row.status}; cannot cancel.",
            details={"reason": "ALREADY_TERMINAL", "status": row.status},
        )

    # We need a cancel-signal channel to GUARANTEE the running task observes the request.
    valkey = _valkey_client(request)
    if valkey is None:
        # No CONFIGURED Valkey -> no signal channel -> cannot guarantee cancellation.
        metrics.task_cancels_total.labels("unavailable").inc()
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Cancel-signal store is not available.")

    settings = get_settings()
    try:
        await valkey.set_cancel_signal(
            prefix=settings.task_signal_key_prefix,
            tenant_id=principal.tenant_id,
            task_id=task_id,
            ttl_seconds=settings.cancel_signal_ttl_seconds,
            timeout_seconds=settings.task_signal_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — configured Valkey erroring: cannot guarantee -> 503
        metrics.task_cancels_total.labels("unavailable").inc()
        logger.warning("cancel_signal_set_failed", task_id=task_id, error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Unable to record the cancel request."
        ) from exc

    metrics.task_cancels_total.labels("accepted").inc()
    logger.info("task_cancel_requested", task_id=task_id, tenant_id=principal.tenant_id)
    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "cancel_requested",
            "message": "Cancellation requested; the task will stop at the next stage boundary.",
        },
    )


# ── Response assembly ─────────────────────────────────────────────────────────────
def _response_from_context(ctx: PipelineContext) -> JSONResponse:
    """Build the HTTP response for a freshly-executed task.

    A GUARDRAIL_VIOLATION short-circuit is surfaced as an HTTP 422 Contract 2 envelope;
    every other outcome is a Contract 3 task-response body (HTTP 200).
    """
    terminal = ctx.terminal_error
    if terminal is not None and terminal.code == _GUARDRAIL_CODE:
        raise ApiError(
            ErrorCode.GUARDRAIL_VIOLATION,
            terminal.message,
            details={"task_id": ctx.task.task_id, "trace_id": ctx.trace_id},
        )

    status = _terminal_status(ctx)
    duration_ms = int((time.monotonic() - ctx.started_monotonic) * 1000)
    task_steps = _build_steps(ctx.steps.steps if ctx.steps is not None else [])

    error: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    if status == "completed":
        output = {"message": ctx.final_answer}
    elif terminal is not None:
        error = _error_envelope(terminal.code, terminal.message, ctx.trace_id, ctx.request_id)

    response = a2a.build_task_response(
        task_id=ctx.task.task_id,
        status=status,
        trace_id=ctx.trace_id,
        started_at=ctx.started_at,
        task_steps=task_steps,
        completed_at=_now_iso(),
        duration_ms=duration_ms,
        tokens_used=ctx.tokens_used,
        cost_usd=ctx.cost_usd,
        output=output,
        error=error,
    )
    return JSONResponse(content=response)


def _response_from_task_row(row: TaskRow, steps: list[StepRow]) -> dict[str, Any]:
    """Rebuild the Contract 3 response from a persisted task row + its audit steps.

    HONEST projection (WP02 amended fix): a non-terminal task reports its REAL current
    status (``pending`` / ``running``) with the audit steps written so far — it is never
    coerced to a fake terminal status (the old fallback coerced ``running`` to
    ``failed``). ``_A2A_STATUSES`` carries the full Contract-3 status set incl. the
    non-terminal values per the amended plan.
    """
    task_steps = _build_steps(steps)
    error: dict[str, Any] | None = None
    output: dict[str, Any] | None = row.output
    if row.status != "completed" and row.error_code is not None:
        error = _error_envelope(
            row.error_code,
            row.error_msg or "Task failed.",
            row.trace_id,
            trace.request_id_var.get(),
        )

    duration_ms: int | None = None
    if row.started_at and row.completed_at:
        duration_ms = _duration_ms(row.started_at, row.completed_at)

    return a2a.build_task_response(
        task_id=row.task_id,
        status=row.status if row.status in _A2A_STATUSES else "failed",
        trace_id=row.trace_id,
        started_at=row.started_at or row.created_at or _now_iso(),
        task_steps=task_steps,
        completed_at=row.completed_at,
        duration_ms=duration_ms,
        tokens_used=row.tokens_used if row.tokens_used is not None else 0,
        cost_usd=row.cost_usd if row.cost_usd is not None else 0.0,
        output=output,
        error=error,
        metadata=row.metadata,
    )


# Contract-3 task statuses (amended plan): the terminal four PLUS the non-terminal
# pending/running so GET never fakes a terminal status for an in-flight task.
_A2A_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled", "timeout"})


def _build_steps(steps: list[StepRow]) -> list[dict[str, Any]]:
    """Project ordered audit StepRows into Contract 3 ``task_steps`` (FIX 2 applied)."""
    return [
        a2a.build_step(
            step_name=step.step_name,
            status=step.status,
            duration_ms=step.duration_ms,
            tokens=step.tokens_used,
            step_type=step.step_type,
            output=step.output,
        )
        for step in steps
    ]


def _error_envelope(code: str, message: str, trace_id: str, request_id: str) -> dict[str, Any]:
    """Build the Contract 2 error shape embedded in a non-completed Contract 3 response."""
    return {
        "code": code,
        "message": message,
        "request_id": request_id,
        "trace_id": trace_id,
        "timestamp": _now_iso(),
    }


def _user_id_from_claims(principal: Principal) -> str | None:
    """Extract the optional EXPLICIT ``user_id`` claim from the verified JWT.

    ONLY an explicit ``user_id`` claim counts — the JWT-``sub`` fallback is REMOVED
    (amended minor): the subject is the agent/principal, not an end-user; falling back
    to it conflated the two and mis-scoped user-scope memory. A sub-only token yields
    ``user_id`` NULL on the task row.
    """
    claims = principal.raw_claims or {}
    user_id = claims.get("user_id")
    return str(user_id) if user_id else None


def _duration_ms(started_at: str, completed_at: str) -> int | None:
    """Best-effort wall-clock ms between two RFC 3339 timestamps; None if unparseable."""
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (end - start).total_seconds() * 1000
    return int(delta) if delta >= 0 else 0


# ── WP08: Auth layer-B authorize on submit ─────────────────────────────────────────
async def _authorize_submit(
    request: Request, principal: Principal, settings: Settings, valkey: Any | None
) -> None:
    """Check ``task:execute`` at Auth (cached verdict, fail-open). Raises 403 on deny.

    The ``AuthClient.authorize`` helper owns the cache + fail-open posture; a raised
    FORBIDDEN propagates as a 403 Contract-2 envelope. Any UNEXPECTED error here is
    swallowed (fail-open) so the authorize layer can never become an availability risk —
    the inbound JWT was already cryptographically verified upstream.
    """
    if not settings.authorize_enabled:
        return
    auth_client = _auth_client(request)
    try:
        await auth_client.authorize(
            tenant_id=principal.tenant_id,
            agent_id=principal.agent_id or "",
            action=settings.authorize_action,
            agent_jwt=principal.raw_token,
            valkey=valkey,
        )
    except ApiError:
        raise  # a definitive FORBIDDEN deny — propagate as 403
    except Exception as exc:  # noqa: BLE001 — unexpected: fail open (availability wins)
        metrics.authorize_checks_total.labels("fail_open").inc()
        logger.warning("authorize_unexpected_error_fail_open", error=str(exc))


# ── WP08: Contract-9 idempotency ────────────────────────────────────────────────────
def _request_fingerprint(body: Any) -> str:
    """Stable SHA-256 over the canonical request body — the Contract-9 conflict discriminator.

    Two submits on the same Idempotency-Key are "the same request" iff their bodies hash
    equal; a differing hash is a key-reuse conflict (409). Uses the validated model's JSON
    projection with sorted keys so field order / whitespace never changes the fingerprint.
    """
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _idempotency_begin(
    principal: Principal,
    settings: Settings,
    valkey: Any | None,
    idem_key: str | None,
    fingerprint: str | None = None,
) -> tuple[JSONResponse | None, Any]:
    """Reserve the idempotency key (or short-circuit). Returns ``(replay, finish)``.

    ``replay`` is a ready :class:`JSONResponse` to return immediately (a stored completed
    response, replayed); ``None`` means proceed with execution. ``finish`` is an async
    callback ``finish(response)`` the caller invokes once the task is terminal to store
    the response for future replays (or release a stuck reservation).

    DISABLED / unit case (no CONFIGURED Valkey, feature flag off, or no Idempotency-Key):
    returns ``(None, <no-op finish>)`` — execution proceeds with no guarantee (allow).

    FAIL-CLOSED: a CONFIGURED Valkey that ERRORS on the reservation raises 503 — we do
    NOT proceed without the idempotency guarantee.

    409: a duplicate request whose original is still ``in_flight`` raises CONFLICT.
    """

    async def _noop_finish(_response: JSONResponse) -> None:
        return None

    if not settings.idempotency_enabled or valkey is None or not idem_key:
        if idem_key and valkey is None:
            metrics.idempotency_requests_total.labels("disabled").inc()
        return None, _noop_finish

    try:
        existing = await valkey.idempotency_reserve(
            prefix=settings.task_signal_key_prefix,
            tenant_id=principal.tenant_id,
            key=idem_key,
            ttl_seconds=settings.idempotency_ttl_seconds,
            timeout_seconds=settings.task_signal_valkey_timeout_seconds,
            fingerprint=fingerprint,
        )
    except Exception as exc:  # noqa: BLE001 — CONFIGURED Valkey errored: FAIL-CLOSED 503
        metrics.idempotency_requests_total.labels("unavailable").inc()
        logger.warning("idempotency_store_unavailable", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Idempotency store unavailable; refusing to proceed without the guarantee.",
        ) from exc

    if existing is not None:
        # Contract-9 / Contract-15 case 13: the SAME Idempotency-Key carrying a DIFFERENT
        # request body is a key-reuse conflict -> 409 IDEMPOTENCY_KEY_CONFLICT (checked before
        # replay/in-flight handling). A legacy record with no stored fingerprint never conflicts.
        if (
            fingerprint is not None
            and existing.fingerprint is not None
            and existing.fingerprint != fingerprint
        ):
            metrics.idempotency_requests_total.labels("conflict").inc()
            logger.info("idempotency_key_conflict", tenant_id=principal.tenant_id)
            raise ApiError(
                ErrorCode.CONFLICT,
                "Idempotency-Key was already used with a different request body.",
                details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
            )
        if existing.state == "completed" and existing.response is not None:
            metrics.idempotency_requests_total.labels("replay").inc()
            logger.info("idempotency_replay", tenant_id=principal.tenant_id)
            return (
                JSONResponse(
                    status_code=existing.status_code or 200,
                    content=existing.response,
                    headers={"Idempotent-Replayed": "true"},
                ),
                _noop_finish,
            )
        # Still in_flight (or unknown): a concurrent duplicate is in progress -> 409.
        metrics.idempotency_requests_total.labels("conflict").inc()
        logger.info("idempotency_conflict", tenant_id=principal.tenant_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "A request with this Idempotency-Key is already in progress.",
            details={"reason": "IDEMPOTENCY_IN_FLIGHT"},
        )

    metrics.idempotency_requests_total.labels("new").inc()

    async def _finish(response: JSONResponse) -> None:
        # Store the terminal response for replay. status_code/body come off the JSONResponse.
        try:
            body = json.loads(bytes(response.body))
        except (ValueError, TypeError):
            body = None
        if body is None:
            # Could not capture the body to replay -> release the reservation so a retry
            # proceeds rather than being wrongly 409'd for the whole TTL.
            await valkey.idempotency_release(
                prefix=settings.task_signal_key_prefix,
                tenant_id=principal.tenant_id,
                key=idem_key,
                timeout_seconds=settings.task_signal_valkey_timeout_seconds,
            )
            return
        await valkey.idempotency_complete(
            prefix=settings.task_signal_key_prefix,
            tenant_id=principal.tenant_id,
            key=idem_key,
            status_code=response.status_code,
            response=body,
            ttl_seconds=settings.idempotency_ttl_seconds,
            timeout_seconds=settings.task_signal_valkey_timeout_seconds,
            fingerprint=fingerprint,
        )

    return None, _finish


# ── WP08: cooperative cancel plumbing ───────────────────────────────────────────────
def _make_cancel_check(
    valkey: Any | None, settings: Settings, tenant_id: str, task_id: str
) -> Any:
    """Build the pipeline's cancel predicate (or None when no cancel store is configured).

    The returned coroutine reads the Valkey cancel key; it RAISES on a Valkey error, and
    the pipeline's ``is_cancel_requested`` treats a raise as "cannot confirm" -> proceeds
    (the running task is never killed by a transient cancel-store blip; the timeout +
    sweeper remain the backstop).
    """
    if valkey is None:
        return None

    async def _check() -> bool:
        return await valkey.is_cancelled(
            prefix=settings.task_signal_key_prefix,
            tenant_id=tenant_id,
            task_id=task_id,
            timeout_seconds=settings.task_signal_valkey_timeout_seconds,
        )

    return _check


async def _clear_cancel(valkey: Any | None, settings: Settings, tenant_id: str, task_id: str) -> None:
    """Best-effort delete of the cancel key once a task is terminal (never raises)."""
    if valkey is None:
        return
    await valkey.clear_cancel_signal(
        prefix=settings.task_signal_key_prefix,
        tenant_id=tenant_id,
        task_id=task_id,
        timeout_seconds=settings.task_signal_valkey_timeout_seconds,
    )


async def _finalise_after_timeout(ctx: PipelineContext, settings: Settings) -> None:
    """Run the EVENT stage to persist the terminal (timeout) state after a budget overrun.

    The ``asyncio.timeout`` cancelled the pipeline mid-run, so EVENT never executed inside
    it. Run it now (outside the timeout) so the task row is finalised + the terminal event
    emitted. EVENT is fail-soft (it logs + swallows its own errors), so this never raises.
    """
    event_stage = _stages.EventStage(producer_version=settings.service_version)
    try:
        await event_stage.run(ctx)
    except Exception as exc:  # noqa: BLE001 — EVENT must never crash the response
        logger.error("timeout_finalise_event_failed", task_id=ctx.task.task_id, error=str(exc))


# ── WP12: async mode + session registration ─────────────────────────────────────────
# Terminal task statuses (no further progress) — the SSE relay closes once a row reaches one.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})


def _resolve_async_mode(mode: str, settings: Settings) -> bool:
    """Resolve the ``?mode=`` query param to a sync/async decision (default sync).

    ``sync`` (or absent) -> False (run inline; unchanged default behaviour). ``async`` ->
    True when the feature flag is on, else 422 (the env disabled the background path). Any
    other value is a 422 VALIDATION_ERROR (the body model already governs the body ``mode``;
    this query param is the WP12 async opt-in, kept separate so the stage-owned request
    model's ``mode`` Literal is untouched).
    """
    normalized = (mode or "sync").lower()
    if normalized == "sync":
        return False
    if normalized == "async":
        if not settings.async_mode_enabled:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Async task mode is not enabled.",
                details={"reason": "ASYNC_MODE_DISABLED"},
            )
        return True
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        "mode query param must be 'sync' or 'async'.",
        details={"reason": "BAD_MODE", "mode": mode},
    )


def _track_background_task(app: Any, task: asyncio.Task[Any]) -> None:
    """Keep a strong reference to a fire-and-forget task so it is not GC'd mid-run.

    asyncio only holds a WEAK reference to a task; without an owning reference the event
    loop can garbage-collect a still-running background task. We pin it on app.state and
    drop it on completion so the set never grows unbounded.
    """
    pending: set[asyncio.Task[Any]] = getattr(app.state, "_async_task_runs", None)
    if pending is None:
        pending = set()
        app.state._async_task_runs = pending
    pending.add(task)
    task.add_done_callback(pending.discard)


async def _register_session(request: Request, principal: Principal, session_id: str) -> None:
    """Register a conversational session idempotently (a session-per-principal, WP12).

    The DURABLE session record is the persisted ``tasks.session_id`` (written by
    ``create_task`` above) — that is the source of truth and is inherently idempotent (the
    same id on N tasks). This additionally registers the session with the sessions/memory
    concept WHEN a memory client is wired on ``app.state`` (the memory stages are Phase 6 /
    not yet wired in this WP), so it is a best-effort, fail-soft no-op today.

    COORDINATION NOTE: the dedicated ``xagent.sessions`` table + the memory-service session
    endpoint land with the stage agent's migration / the Phase-6 memory wiring; this hook is
    structured to call them the moment ``app.state.memory_client`` exists, with NO behaviour
    change to the task path when it does not.
    """
    memory_client = getattr(request.app.state, "memory_client", None)
    if memory_client is None:
        logger.debug("session_registered_on_task_only", session_id=session_id)
        return
    register = getattr(memory_client, "register_session", None)
    if register is None:
        return
    try:
        await register(
            session_id=session_id,
            agent_jwt=principal.raw_token,
            on_behalf_of=principal.agent_id,
        )
    except Exception as exc:  # noqa: BLE001 — session registration is best-effort, never blocks
        logger.warning("session_register_failed", session_id=session_id, error=str(exc))


def _async_terminal_response(ctx: PipelineContext) -> JSONResponse:
    """Build the terminal Contract-3 body for an async run (never raises a 422 envelope).

    The sync ``_response_from_context`` RAISES ApiError(422) on a GUARDRAIL_VIOLATION (an
    HTTP error envelope). In the background there is no live HTTP response to error, so a
    blocked task is rendered as a Contract-3 ``failed`` body (the same shape GET would
    return) instead — the guardrail block is already persisted as the terminal task state.
    """
    terminal = ctx.terminal_error
    if terminal is not None and terminal.code == _GUARDRAIL_CODE:
        task_steps = _build_steps(ctx.steps.steps if ctx.steps is not None else [])
        error = _error_envelope(terminal.code, terminal.message, ctx.trace_id, ctx.request_id)
        body = a2a.build_task_response(
            task_id=ctx.task.task_id,
            status="failed",
            trace_id=ctx.trace_id,
            started_at=ctx.started_at,
            task_steps=task_steps,
            completed_at=_now_iso(),
            duration_ms=int((time.monotonic() - ctx.started_monotonic) * 1000),
            tokens_used=ctx.tokens_used,
            cost_usd=ctx.cost_usd,
            error=error,
        )
        return JSONResponse(content=body)
    return _response_from_context(ctx)


# ── WP12: SSE event publishing (producer side) + relay (consumer side) ───────────────
def _make_event_publisher(
    valkey: Any | None, settings: Settings, tenant_id: str, task_id: str
) -> Any:
    """Build a bound ``publish(event: dict)`` coroutine for the pipeline to call (or None).

    Returned as ``ctx.publish_event`` so a stage can push a live SSE frame without knowing
    the channel or Valkey. None / no-publish-capable Valkey yields a no-op publisher, so the
    pipeline never has to special-case the Valkey-absent (test / degraded) environment.
    """
    async def _publish(event: dict[str, Any]) -> None:
        await _publish_event(valkey, settings, tenant_id, task_id, event)

    return _publish


async def _publish_event(
    valkey: Any | None,
    settings: Settings,
    tenant_id: str,
    task_id: str,
    event: dict[str, Any],
) -> None:
    """Publish one SSE frame to the per-task channel (best-effort; no-op when unavailable).

    A no-op when no CONFIGURED Valkey exposes ``publish_task_event`` (the unit / degraded
    case): SSE then relies purely on the polling fallback. Never raises.
    """
    if valkey is None or not hasattr(valkey, "publish_task_event"):
        return
    try:
        await valkey.publish_task_event(
            prefix=settings.task_signal_key_prefix,
            tenant_id=tenant_id,
            task_id=task_id,
            event=event,
            timeout_seconds=settings.sse_publish_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — publish is soft; the poll fallback covers it
        logger.warning("sse_publish_failed", task_id=task_id, error=str(exc))


def _progress_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Build a progress/lifecycle SSE event frame (``{event, ...}``)."""
    return {"event": event_type, **fields}


def _terminal_event_from_response(task_id: str, response: JSONResponse) -> dict[str, Any]:
    """Derive the terminal SSE frame (``done`` / ``error`` / ``content_filter``) from a body.

    A completed task -> ``done`` (carrying the output); a guardrail block -> ``content_filter``
    (terminal); any other non-completed status -> ``error`` (carrying the Contract-2 error).
    """
    try:
        body = json.loads(bytes(response.body))
    except (ValueError, TypeError):
        body = {"task_id": task_id, "status": "failed"}
    return _terminal_event_from_body(task_id, body)


def _terminal_event_from_body(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Map a Contract-3 task body to its terminal SSE frame type + payload."""
    status = body.get("status", "failed")
    error = body.get("error") or {}
    if status == "completed":
        return _progress_event("done", task_id=task_id, status=status, result=body)
    if error.get("code") == _GUARDRAIL_CODE:
        # A guardrail blocked the task — a content_filter terminal event (then close).
        return _progress_event(
            "content_filter", task_id=task_id, status=status, error=error, result=body
        )
    return _progress_event("error", task_id=task_id, status=status, error=error, result=body)


def _sse_frame(event: dict[str, Any]) -> str:
    """Serialise an event dict to the SSE wire format (``event:``/``data:`` lines)."""
    event_type = event.get("event", "message")
    data = json.dumps(event, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"


def _snapshot_event(row: TaskRow, steps: list[StepRow]) -> dict[str, Any]:
    """Build a ``snapshot`` SSE frame from a task row + its steps (the polling fallback)."""
    return _progress_event(
        "snapshot",
        task_id=row.task_id,
        status=row.status,
        task_steps=[
            {"step": s.step_name, "status": a2a.map_step_status(s.status)} for s in steps
        ],
        tokens_used=row.tokens_used or 0,
        cost_usd=row.cost_usd or 0.0,
    )


async def _sse_event_stream(
    *,
    pool: Any,
    valkey: Any | None,
    settings: Settings,
    tenant_id: str,
    task_id: str,
    initial_row: TaskRow,
) -> AsyncIterator[bytes]:
    """The SSE generator: relay live Pub/Sub frames, falling back to row/step polling.

    Strategy:
      1. Emit an initial ``snapshot`` so a late subscriber immediately sees current state
         (and a task that is ALREADY terminal yields its terminal frame + closes at once).
      2. If a CONFIGURED Valkey exposes ``subscribe_task_events``, relay published frames
         live; a terminal frame (done/error/content_filter) closes the stream. A periodic
         poll also runs alongside so a terminal state is never missed if the terminal
         publish was lost (the row is the source of truth).
      3. If Pub/Sub is unavailable (Valkey absent / subscribe error), FALL BACK to polling
         the row + steps every ``sse_poll_interval_seconds`` and emitting ``snapshot``
         frames until the task is terminal — degraded but functional with no infra.
    A hard ``sse_max_duration_seconds`` ceiling closes a stream whose task never finishes.
    """
    started = time.monotonic()

    # 1) Initial snapshot (covers the already-terminal case in one frame + close).
    steps = await _safe_list_steps(pool, tenant_id, task_id)
    yield _sse_frame(_snapshot_event(initial_row, steps)).encode("utf-8")
    if initial_row.status in _TERMINAL_STATUSES:
        yield _sse_frame(_terminal_for_row(initial_row, steps)).encode("utf-8")
        return

    # 2) Live Pub/Sub relay when a configured Valkey supports it.
    if valkey is not None and hasattr(valkey, "subscribe_task_events"):
        async for chunk in _sse_pubsub_relay(
            pool=pool, valkey=valkey, settings=settings, tenant_id=tenant_id,
            task_id=task_id, started=started,
        ):
            yield chunk
        return

    # 3) Polling fallback (no Pub/Sub): snapshot until terminal or the duration ceiling.
    async for chunk in _sse_poll_relay(
        pool=pool, settings=settings, tenant_id=tenant_id, task_id=task_id, started=started
    ):
        yield chunk


async def _sse_pubsub_relay(
    *,
    pool: Any,
    valkey: Any,
    settings: Settings,
    tenant_id: str,
    task_id: str,
    started: float,
) -> AsyncIterator[bytes]:
    """Relay live Pub/Sub frames; a terminal frame (or a terminal row) closes the stream.

    A background poll task guards against a lost terminal publish: each ``subscribe`` frame
    is raced against a poll timeout, and on each timeout the row is re-checked so a task that
    finished without (or despite) a terminal publish still closes the stream. On any Pub/Sub
    error we degrade to the polling fallback rather than dropping the client.
    """
    try:
        subscription = valkey.subscribe_task_events(
            prefix=settings.task_signal_key_prefix, tenant_id=tenant_id, task_id=task_id
        )
        agen = subscription.__aiter__()
        while True:
            if time.monotonic() - started > settings.sse_max_duration_seconds:
                yield _sse_frame(_progress_event(
                    "error", task_id=task_id, status="timeout",
                    error={"code": ErrorCode.SERVICE_UNAVAILABLE, "message": "Stream timed out."},
                )).encode("utf-8")
                with contextlib.suppress(Exception):
                    await agen.aclose()
                return
            try:
                event = await asyncio.wait_for(
                    agen.__anext__(), timeout=settings.sse_poll_interval_seconds
                )
            except TimeoutError:
                # No live frame this tick — reconcile against the row (catch a missed/lost
                # terminal publish so the stream still closes when the task is done).
                row = await tasks_repo.get_task(pool, tenant_id, task_id)
                if row is not None and row.status in _TERMINAL_STATUSES:
                    steps = await _safe_list_steps(pool, tenant_id, task_id)
                    yield _sse_frame(_terminal_for_row(row, steps)).encode("utf-8")
                    with contextlib.suppress(Exception):
                        await agen.aclose()
                    return
                yield b": keep-alive\n\n"  # SSE comment heartbeat (keeps the connection warm)
                continue
            except StopAsyncIteration:
                return
            yield _sse_frame(event).encode("utf-8")
            if event.get("event") in ("done", "error", "content_filter"):
                with contextlib.suppress(Exception):
                    await agen.aclose()
                return
    except Exception as exc:  # noqa: BLE001 — Pub/Sub failed: degrade to the poll fallback
        logger.warning("sse_pubsub_relay_failed", task_id=task_id, error=str(exc))
        async for chunk in _sse_poll_relay(
            pool=pool, settings=settings, tenant_id=tenant_id, task_id=task_id, started=started
        ):
            yield chunk


async def _sse_poll_relay(
    *,
    pool: Any,
    settings: Settings,
    tenant_id: str,
    task_id: str,
    started: float,
) -> AsyncIterator[bytes]:
    """Poll the task row + steps, emitting ``snapshot`` frames until terminal (fallback)."""
    while True:
        if time.monotonic() - started > settings.sse_max_duration_seconds:
            yield _sse_frame(_progress_event(
                "error", task_id=task_id, status="timeout",
                error={"code": ErrorCode.SERVICE_UNAVAILABLE, "message": "Stream timed out."},
            )).encode("utf-8")
            return
        row = await tasks_repo.get_task(pool, tenant_id, task_id)
        if row is None:
            # The row vanished mid-stream (should not happen) — close cleanly.
            yield _sse_frame(_progress_event(
                "error", task_id=task_id, status="failed",
                error={"code": ErrorCode.NOT_FOUND, "message": "Task disappeared."},
            )).encode("utf-8")
            return
        steps = await _safe_list_steps(pool, tenant_id, task_id)
        # Emit a snapshot on every poll (cheap; lets a client render incremental steps).
        yield _sse_frame(_snapshot_event(row, steps)).encode("utf-8")
        if row.status in _TERMINAL_STATUSES:
            yield _sse_frame(_terminal_for_row(row, steps)).encode("utf-8")
            return
        await asyncio.sleep(settings.sse_poll_interval_seconds)


def _terminal_for_row(row: TaskRow, steps: list[StepRow]) -> dict[str, Any]:
    """Build the terminal SSE frame for an already-terminal task row (done/error/filter)."""
    body = _response_from_task_row(row, steps)
    return _terminal_event_from_body(row.task_id, body)


async def _safe_list_steps(pool: Any, tenant_id: str, task_id: str) -> list[StepRow]:
    """List a task's steps, swallowing errors to an empty list (the stream must not break)."""
    try:
        return await steps_repo.list_steps(pool, tenant_id, task_id)
    except Exception as exc:  # noqa: BLE001 — a step-read blip must not kill the stream
        logger.warning("sse_list_steps_failed", task_id=task_id, error=str(exc))
        return []


# ── WP08: list projection + cursor codec ────────────────────────────────────────────
def _list_item_to_json(item: tasks_repo.TaskListItem) -> dict[str, Any]:
    """Project a redaction-safe task summary for the Task Feed list (no input/output)."""
    return {
        "task_id": item.task_id,
        "agent_id": item.agent_id,
        "status": item.status,
        "trace_id": item.trace_id,
        "error_code": item.error_code,
        "tokens_used": item.tokens_used,
        "cost_usd": item.cost_usd,
        "metadata": item.metadata,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def _encode_cursor(item: tasks_repo.TaskListItem) -> str:
    """Encode the keyset cursor ``(created_at, task_id)`` as an opaque base64url token."""
    raw = json.dumps({"c": item.created_at, "t": item.task_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """Decode an opaque list cursor into ``(created_at, task_id)``; (None, None) if absent.

    A malformed cursor is a client error (422) rather than a silent first-page reset, so a
    paging bug surfaces instead of quietly looping the first page.
    """
    if not cursor:
        return None, None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return str(data["c"]), str(data["t"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Malformed pagination cursor.",
            details={"reason": "BAD_CURSOR"},
        ) from exc
