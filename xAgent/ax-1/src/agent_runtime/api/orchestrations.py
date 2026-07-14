"""Orchestration run API (phase B5) — PROMPT -> ORCHESTRATOR -> SUB-AGENTS over HTTP.

  POST   /v1/orchestrations            submit a goal; drives in the background -> 202 {workflow_id}
  GET    /v1/orchestrations            list this orchestrator's runs
  GET    /v1/orchestrations/{id}       run status + output
  GET    /v1/orchestrations/{id}/graph run + the node tree (the execution graph)
  GET    /v1/orchestrations/{id}/stream SSE run-tree (polls run + nodes until terminal)
  DELETE /v1/orchestrations/{id}       cancel (sets the workflow cancel flag)

Orchestrator-only (the coordinator enforces `agent_type='orchestrator'`). This router is ADDITIVE —
it never touches the single-agent task path. It reuses the app's existing clients via app.state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core import trace
from ..core.auth import Principal, require_principal
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..core.validation import parse_uuid_path
from ..models import a2a
from ..orchestration import repo
from ..orchestration.service import OrchestrationCoordinator
from ..services.auth_client import AuthClient

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["orchestrations"])

_LIST_DEFAULT = 50
_LIST_MAX = 200
_STREAM_POLL_SECONDS = 1.0
_STREAM_MAX_SECONDS = 900.0
_TERMINAL = frozenset({"completed", "failed", "cancelled", "timeout"})


class OrchestrationRequest(BaseModel):
    """POST /v1/orchestrations body."""

    model_config = {"extra": "forbid"}
    goal: str = Field(min_length=1, max_length=8000)
    mode: str = Field(default="subagents", pattern="^(subagents|solo)$")
    #: RUN-level tool switch, independent of ``mode``. False => every task in the run is a plain chat
    #: completion: no tool is resolved, offered or invoked, and the planner is shown a toolless
    #: roster. With ``mode="solo"`` this is the plain-chatbot configuration. Defaults True (the LLM
    #: sees the tools its agent holds and decides for itself whether to call them).
    use_tools: bool = True
    cost_budget_usd: float | None = Field(default=None, gt=0)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Orchestration store is not available.")
    return pool


def _coordinator(request: Request) -> OrchestrationCoordinator:
    """Return the app's orchestration coordinator, building it lazily from app.state if absent."""
    existing = getattr(request.app.state, "orchestration_coordinator", None)
    if existing is not None:
        return existing
    st = request.app.state
    settings = getattr(st, "settings", None) or get_settings()
    auth_client = getattr(st, "auth_client", None) or AuthClient(settings, st.token_provider)
    st.auth_client = auth_client
    coord = OrchestrationCoordinator(
        pool=_pool(request), settings=settings, valkey=st.valkey, llms_client=st.llms_client,
        auth_client=auth_client, hil_client=getattr(st, "hil_client", None),
    )
    st.orchestration_coordinator = coord
    return coord


def _track_background(app: Any, task: asyncio.Task[Any]) -> None:
    """Pin a fire-and-forget task so the loop can't GC it mid-run; drop it on completion."""
    pending: set[asyncio.Task[Any]] = getattr(app.state, "_orchestration_runs", None) or set()
    pending.add(task)
    app.state._orchestration_runs = pending
    task.add_done_callback(pending.discard)


def _workflow_dict(wf: repo.WorkflowRow) -> dict[str, Any]:
    return {
        "workflow_id": wf.workflow_id, "tenant_id": wf.tenant_id, "root_agent_id": wf.root_agent_id,
        "goal": wf.goal, "status": wf.status, "mode": wf.mode, "use_tools": wf.use_tools,
        "decomposition": wf.decomposition,
        "output": wf.output, "error_code": wf.error_code, "error_msg": wf.error_msg,
        "tokens_used": wf.tokens_used, "cost_usd": wf.cost_usd, "cost_budget_usd": wf.cost_budget_usd,
        "created_at": wf.created_at, "started_at": wf.started_at, "completed_at": wf.completed_at,
    }


def _node_steps(
    task_id: str | None, steps_by_task: dict[str, list[repo.NodeStepRow]]
) -> list[dict[str, Any]]:
    """Project a node's audit steps onto the wire — the sub-agent's actual work, tool calls included.

    Uses :func:`a2a.build_step`, the SAME projection the single-agent task API uses, and for the same
    reason: it is an ALLOW-LIST. A ``tool_call`` step exposes only ``tool`` / ``tool_version`` /
    ``tool_call_id`` / ``error``; every other step type's raw ``output`` JSONB stays server-side (a
    guardrail step's ``violations`` can carry the matched content). Never hand-roll this projection.
    """
    if not task_id:
        return []
    return [
        a2a.build_step(
            step_name=s.step_name,
            status=s.status,
            duration_ms=s.duration_ms,
            tokens=s.tokens_used,
            step_type=s.step_type,
            output=s.output,
        )
        for s in steps_by_task.get(task_id, [])
    ]


def _node_dict(
    n: repo.WorkflowTaskRow, steps_by_task: dict[str, list[repo.NodeStepRow]] | None = None
) -> dict[str, Any]:
    return {
        "node_id": n.node_id, "node_type": n.node_type, "status": n.status,
        "assigned_agent_id": n.assigned_agent_id, "preset": n.preset, "depends_on": n.depends_on,
        "task_id": n.task_id, "output": n.output, "tokens_used": n.tokens_used, "cost_usd": n.cost_usd,
        "started_at": n.started_at, "completed_at": n.completed_at,
        # What this sub-agent actually DID (guardrails -> llm -> tool_call... ). Carried inline so the
        # run tree streams tool calls AS THEY HAPPEN, instead of the UI lazily re-fetching each node's
        # task (an N+1 that grew with fan-out, re-fired every poll, and showed nothing at all while a
        # node was still running — which is exactly when you want to watch it).
        "steps": _node_steps(n.task_id, steps_by_task or {}),
    }


@router.post("/orchestrations", response_model=None)
async def submit_orchestration(
    body: OrchestrationRequest, request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    """Create a run (orchestrator-only) and drive it in the background; returns 202 immediately."""
    coord = _coordinator(request)
    workflow = await coord.create_run(
        principal, goal=body.goal, mode=body.mode, use_tools=body.use_tools,
        cost_budget_usd=body.cost_budget_usd, timeout_seconds=body.timeout_seconds,
    )
    trace_id = trace.trace_id_var.get()
    request_id = trace.request_id_var.get()
    runner = asyncio.ensure_future(
        coord.drive(principal, workflow, trace_id=trace_id, request_id=request_id)
    )
    _track_background(request.app, runner)
    return JSONResponse(
        status_code=202,
        content={
            "workflow_id": workflow.workflow_id, "status": "running", "mode": workflow.mode,
            "use_tools": workflow.use_tools,
            "trace_id": trace_id,
            "message": "Run accepted; poll GET /v1/orchestrations/{id} or stream .../stream.",
        },
    )


@router.get("/orchestrations", response_model=None)
async def list_orchestrations(
    request: Request,
    principal: Principal = Depends(require_principal),
    limit: int = _LIST_DEFAULT,
    status: str | None = None,
) -> dict[str, Any]:
    """List this orchestrator's runs, newest-first."""
    pool = _pool(request)
    rows = await repo.list_workflows(
        pool, principal.tenant_id, limit=max(1, min(limit, _LIST_MAX)),
        root_agent_id=principal.agent_id, status=status,
    )
    return {"items": [_workflow_dict(w) for w in rows]}


@router.get("/orchestrations/{workflow_id}", response_model=None)
async def get_orchestration(
    workflow_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict[str, Any]:
    wid = parse_uuid_path(workflow_id, param="Workflow")
    wf = await repo.get_workflow(_pool(request), principal.tenant_id, wid)
    if wf is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Orchestration {wid} not found.")
    return _workflow_dict(wf)


@router.get("/orchestrations/{workflow_id}/graph", response_model=None)
async def get_orchestration_graph(
    workflow_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict[str, Any]:
    """The run + its node tree, each node carrying the sub-agent's own steps (its tool calls)."""
    wid = parse_uuid_path(workflow_id, param="Workflow")
    pool = _pool(request)
    wf = await repo.get_workflow(pool, principal.tenant_id, wid)
    if wf is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Orchestration {wid} not found.")
    nodes = await repo.list_workflow_tasks(pool, principal.tenant_id, wid)
    steps = await repo.list_workflow_steps(pool, principal.tenant_id, wid)
    return {"workflow": _workflow_dict(wf), "nodes": [_node_dict(n, steps) for n in nodes]}


@router.delete("/orchestrations/{workflow_id}", response_model=None)
async def cancel_orchestration(
    workflow_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    """Signal a run to cancel. Terminal runs are a no-op; unknown -> 404."""
    wid = parse_uuid_path(workflow_id, param="Workflow")
    pool = _pool(request)
    wf = await repo.get_workflow(pool, principal.tenant_id, wid)
    if wf is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Orchestration {wid} not found.")
    if wf.status in _TERMINAL:
        return JSONResponse(status_code=200, content={"workflow_id": wid, "status": wf.status, "no_op": True})
    try:
        await _coordinator(request).request_cancel(principal.tenant_id, wid)
    except Exception as exc:  # noqa: BLE001 — cannot guarantee the signal landed
        logger.warning("orchestration_cancel_failed", workflow_id=wid, error=str(exc))
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Could not signal cancel (retry).") from exc
    return JSONResponse(status_code=202, content={"workflow_id": wid, "status": "cancelling"})


@router.get("/orchestrations/{workflow_id}/stream", response_model=None)
async def stream_orchestration(
    workflow_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> StreamingResponse:
    """SSE run-tree: emits ``run`` + ``nodes`` frames as the graph changes, then ``done``.

    Poll-based (decoupled from the driver): the endpoint re-reads the workflow + nodes each tick and
    emits a frame when anything changed, closing after the run reaches a terminal status. 404 for an
    unknown / cross-tenant run (RLS hides it).
    """
    wid = parse_uuid_path(workflow_id, param="Workflow")
    pool = _pool(request)
    wf = await repo.get_workflow(pool, principal.tenant_id, wid)
    if wf is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Orchestration {wid} not found.")
    return StreamingResponse(
        _run_sse(pool, principal.tenant_id, wid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _frame(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


async def _run_sse(pool: Any, tenant_id: str, workflow_id: str) -> AsyncIterator[bytes]:
    """Poll the run + nodes + their steps; emit a frame on change; close on terminal (hard max duration).

    The steps are part of the snapshot, so the change-signature trips on each new audit row — i.e. a
    frame is pushed the moment a sub-agent calls a tool, and the tree animates as the work happens.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _STREAM_MAX_SECONDS
    last = ""
    while loop.time() < deadline:
        wf = await repo.get_workflow(pool, tenant_id, workflow_id)
        if wf is None:
            yield _frame("error", {"error": {"code": "NOT_FOUND", "message": "Run disappeared."}})
            return
        nodes = await repo.list_workflow_tasks(pool, tenant_id, workflow_id)
        steps = await repo.list_workflow_steps(pool, tenant_id, workflow_id)
        snapshot = {"workflow": _workflow_dict(wf), "nodes": [_node_dict(n, steps) for n in nodes]}
        signature = json.dumps(snapshot, sort_keys=True, default=str)
        if signature != last:
            last = signature
            yield _frame("run", snapshot)
        if wf.status in _TERMINAL:
            yield _frame("done", {"workflow_id": workflow_id, "status": wf.status, "output": wf.output})
            return
        await asyncio.sleep(_STREAM_POLL_SECONDS)
    yield _frame("error", {"error": {"code": "TIMEOUT", "message": "Run stream exceeded max duration."}})
