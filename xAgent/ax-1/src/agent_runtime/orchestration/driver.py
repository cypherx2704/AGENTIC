"""DAG driver (phase B2c) — decompose a goal, fan out its nodes to sub-agents, synthesize.

This is the orchestration heart for ``mode = subagents``: it drives a validated DAG layer by layer
(``dag.topological_layers``), running each node's assigned sub-agent CONCURRENTLY within a layer
(``asyncio.gather``), threading each node's SUMMARY (never its transcript) into downstream nodes'
input, applying ``on_error`` per node, and recording every transition on ``xagent.workflow_tasks``
with optimistic locking. The orchestrator running ALONE (``mode = solo``) is a direct
single-agent run handled by the run endpoint (B5), not this driver.

Node -> sub-agent binding (committed model): ``node.assigned_agent_id`` wins; else ``node.preset``
resolves to the roster's sub-agent for that preset (presets materialize as concrete sub-agents —
reuses the existing sub-agent CRUD + per-sub-agent runtime config). A node that resolves to no
sub-agent fails (``UNASSIGNED_NODE``).

The pure scheduling/binding/message-rendering helpers are unit-tested; :func:`run_workflow` is the
integration loop (needs the DB + executor + pipeline) and is exercised by the review + a future
integration test. Budget ceiling (B3) and HIL gating (B4) layer on top of this in later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Container, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from . import dag as dagmod
from . import repo
from .dag import DagNode
from .executor import SubAgentResult, SubAgentTokenProvider, run_subagent_task

logger = structlog.get_logger(__name__)

#: A fail-safe async cancel predicate — returns True iff the workflow should stop (Valkey cancel /
#: timeout). Polled between layers and passed to each in-flight node so a cancel tears down running
#: sub-agent pipelines too. The signal source (a DELETE endpoint / timeout sweeper) is wired in B5.
CancelCheck = Callable[[], Awaitable[bool]]

#: A HIL gate: given (operation_type, context), returns True to proceed or False when denied/expired.
#: Wired in B5 to HilClient.request_and_wait — auto-approves under the orchestrator's `automated` HIL
#: mode, otherwise pauses and polls for a human verdict (fail-closed). A denied node then flows through
#: its own ``on_error`` policy (so a research node skips, a write node fails — the §11 decision).
HilGate = Callable[[str, dict[str, Any]], Awaitable[bool]]

#: An injectable final-synthesis step: (goal, node_summaries) -> (answer, tokens_used, cost_usd).
#: Wired in B5 to the orchestrator LLM (:func:`llm.synthesize`); usage is accrued to the run total.
#: ``None`` uses the deterministic leaf-join fallback (:func:`_default_synthesis`).
Synthesizer = Callable[[str, dict[str, str]], Awaitable[tuple[str, int, float]]]

_BINDING_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+?)\.output\s*\}\}")


def over_budget(
    spent_cost: float, cost_budget: float | None, spent_tokens: int, token_budget: int | None
) -> bool:
    """True when a per-workflow cost OR token ceiling has been reached (early-stop trigger)."""
    if cost_budget is not None and cost_budget >= 0 and spent_cost >= cost_budget:
        return True
    return token_budget is not None and token_budget >= 0 and spent_tokens >= token_budget


def remaining_cost(cost_budget: float | None, spent_cost: float) -> float | None:
    """The USD budget a next node may spend (so one node can't blow the whole ceiling); None = uncapped."""
    if cost_budget is None:
        return None
    return max(0.0, cost_budget - spent_cost)


async def _safe_cancel(cancel_check: CancelCheck | None) -> bool:
    """Poll the cancel predicate fail-safe (any error -> not cancelled; the run proceeds)."""
    if cancel_check is None:
        return False
    try:
        return await cancel_check()
    except Exception as exc:  # noqa: BLE001 — a cancel-store hiccup must never crash the run
        logger.warning("workflow_cancel_check_failed", error=str(exc))
        return False


def _node_cancelled(result: SubAgentResult | ApiError) -> bool:
    """True when a node result is a CANCELLATION (in-flight torn down, or cancelled at the HIL gate)."""
    if isinstance(result, SubAgentResult):
        return result.status == "cancelled"
    return (result.details or {}).get("reason") == "CANCELLED"


def _deadline_from(timeout_at_iso: str | None) -> datetime | None:
    """Parse an RFC-3339 (…Z) workflow ``timeout_at`` into an aware datetime, or ``None``."""
    if not timeout_at_iso:
        return None
    try:
        return datetime.fromisoformat(timeout_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _past_deadline(deadline: datetime | None) -> bool:
    return deadline is not None and datetime.now(UTC) >= deadline


async def _await_gate(
    hil_gate: HilGate, cancel_check: CancelCheck | None, *, operation: str, context: dict[str, Any]
) -> bool | None:
    """Await a HIL gate while staying cancel-aware: True/False verdict, or ``None`` if cancelled mid-wait.

    A HIL wait can be minutes; without this a workflow cancel during ``awaiting_approval`` would be
    ignored until the human resolves (or the gate's own max-wait elapses). Racing the gate against the
    cancel poll lets DELETE abort a paused node promptly.
    """
    gate_task: asyncio.Task[bool] = asyncio.ensure_future(hil_gate(operation, context))
    try:
        while True:
            done, _pending = await asyncio.wait({gate_task}, timeout=2.0)
            if gate_task in done:
                return gate_task.result()
            if await _safe_cancel(cancel_check):
                return None
    finally:
        if not gate_task.done():
            gate_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown only
                await gate_task

# Terminal node statuses that satisfy a downstream dependency (completed OR skipped — a skipped
# upstream still "resolves" so the DAG can make progress; a failed upstream blocks/aborts).
_DEP_SATISFIED = frozenset({"completed", "skipped"})


# ── pure helpers ─────────────────────────────────────────────────────────────────────────
def resolve_node_agent(
    node: DagNode, roster: dict[str, str], *, default_agent_id: str | None = None
) -> str | None:
    """Resolve the concrete agent id a node runs as.

    The rule, exactly::

        if no agent is specified   -> the default agent (the ORCHESTRATOR)
        elif that agent exists     -> use it
        else                       -> raise UNKNOWN_AGENT

    The asymmetry between the first and last branch IS the design:

    * **No agent specified** — the ``solo`` graph's node. The planner was never asked to route it, so
      the lead agent takes it. It falls back to ``default_agent_id``, which is always the
      orchestrator, never a sub-agent. (``None`` only if there is no default either -> the caller
      raises ``UNASSIGNED_NODE``.)
    * **An agent that does not exist** — the planner *did* decide, and named something that is not
      there (a typo, a hallucination, or a roster that changed mid-run). This RAISES
      ``UNKNOWN_AGENT``. It must never fall through to the default: quietly running the step on some
      other agent would discard the planner's choice and substitute the backend's own, which is the
      one thing this engine forbids. Fail loudly, name the bogus target.

    Raises:
        ApiError: ``UNKNOWN_AGENT`` when ``node.preset`` names an agent absent from ``roster``.
    """
    if node.assigned_agent_id:
        return node.assigned_agent_id
    if node.preset:
        agent_id = roster.get(node.preset)
        if agent_id is None:
            raise ApiError(
                ErrorCode.UNKNOWN_AGENT,
                f"Node {node.node_id!r} names agent {node.preset!r}, which does not exist. "
                f"Known agents: {', '.join(sorted(roster)) or '(none)'}.",
                details={"node_id": node.node_id, "requested_agent": node.preset,
                         "known_agents": sorted(roster)},
            )
        return agent_id
    return default_agent_id


def render_node_message(
    node: DagNode, goal: str, summaries: Mapping[str, str], dep_ids: Iterable[str] = ()
) -> str:
    """Build a node's input message: the goal plus the material it depends on.

    Explicit ``input_bindings`` (``{{<node_id>.output}}`` tokens replaced by that node's summary) win.
    When a node has NO explicit bindings — the common case, since templates AND LLM plans express
    dependencies as EDGES, not bindings — the summaries of the node's direct dependencies (``dep_ids``)
    are auto-appended, so a downstream node always sees its upstreams' findings instead of running
    blind on the bare goal. A node with neither bindings nor resolved upstreams just receives the goal.
    """

    def _sub(match: re.Match[str]) -> str:
        return summaries.get(match.group(1), "")

    if node.input_bindings:
        parts: list[str] = [goal]
        for label, raw in node.input_bindings.items():
            if not isinstance(raw, str):
                continue
            rendered = _BINDING_RE.sub(_sub, raw).strip()
            if rendered:
                parts.append(f"{label}:\n{rendered}")
        return "\n\n".join(parts)

    upstream = [summaries[d] for d in dep_ids if summaries.get(d)]
    if not upstream:
        return goal
    return goal + "\n\nContext from prior steps:\n" + "\n\n".join(upstream)


@dataclass
class NodeState:
    """In-driver view of a node's execution state (mirrors a ``workflow_tasks`` row + its summary)."""

    node: DagNode
    pk: str  # xagent.workflow_tasks.id
    version: int
    status: str = "pending"
    summary: str = ""
    task_id: str | None = None


@dataclass
class WorkflowOutcome:
    """The driver's terminal summary of a run."""

    status: str  # completed | failed | cancelled
    output: dict[str, Any]
    error_code: str | None = None
    error_msg: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    node_summaries: dict[str, str] = field(default_factory=dict)


def dependency_map(dag: dagmod.Dag) -> dict[str, set[str]]:
    """Full per-node dependency set from edges AND node-level ``depends_on`` (deduped)."""
    deps_of: dict[str, set[str]] = {nid: set() for nid in dag.nodes}
    for dep, dependent in dag.dependency_pairs():
        deps_of[dependent].add(dep)
    return deps_of


def deps_satisfied(
    deps: Iterable[str], states: Mapping[str, NodeState], blocked: Container[str] = frozenset()
) -> bool:
    """True when every dependency reached a dep-satisfying terminal status AND is not blocked.

    A dependency that failed, is still pending/running, is missing, OR was cascade-skipped because
    ITS own dependencies were unsatisfied (``blocked``) makes the dependent UNREADY — so an
    unsatisfied-skip propagates transitively rather than a descendant running on empty inputs. An
    INTENTIONAL skip (on_error='skip'/'continue') is NOT blocked, so downstream still runs with the
    partial result (per the §11 skip-node decision).
    """
    for dep in deps:
        st = states.get(dep)
        if st is None or st.status not in _DEP_SATISFIED or dep in blocked:
            return False
    return True


# ── integration driver ───────────────────────────────────────────────────────────────────
async def run_workflow(
    *,
    pool: AsyncConnectionPool,
    settings: Settings,
    token_provider: SubAgentTokenProvider,
    orchestrator: Principal,
    workflow: repo.WorkflowRow,
    roster: dict[str, str],
    trace_id: str,
    request_id: str,
    node_budget_seconds: float,
    cost_budget_usd: float | None = None,
    token_budget: int | None = None,
    cancel_check: CancelCheck | None = None,
    hil_gate: HilGate | None = None,
    synthesizer: Synthesizer | None = None,
    default_agent_id: str | None = None,
    initial_tokens: int = 0,
    initial_cost: float = 0.0,
) -> WorkflowOutcome:
    """Drive a ``subagents`` workflow to completion and return its terminal outcome.

    ``workflow`` is an already-created ``pending`` row whose ``subtask_dag`` has been set. ``roster``
    maps preset name -> sub-agent id. Each node runs via :func:`executor.run_subagent_task`
    (summary-only). Layers run concurrently; a node whose deps did not all succeed is cascade-skipped.

    Budget ceiling (early stop): node token/cost usage accumulates; once it crosses ``cost_budget_usd``
    (defaults to the workflow row's) or ``token_budget``, the run stops AFTER the current layer with
    ``BUDGET_EXCEEDED``. Each node is also capped at the REMAINING cost so one node can't blow the whole
    budget. Cancel/timeout: ``cancel_check`` is polled between layers (-> ``cancelled``) and passed to
    each in-flight node so a cancel tears down running sub-agent pipelines too.
    """
    tid = orchestrator.tenant_id
    cost_budget = cost_budget_usd if cost_budget_usd is not None else workflow.cost_budget_usd

    dag_doc = workflow.subtask_dag or {}
    try:
        dag = dagmod.parse_dag(dag_doc)
        layers = dagmod.validate_dag(dag)
    except ApiError as exc:
        await repo.update_workflow(
            pool, tid, workflow.workflow_id, expected_version=workflow.version, status="failed",
            error_code=exc.code, error_msg=exc.message, mark_completed=True,
        )
        return WorkflowOutcome(status="failed", output={}, error_code=exc.code, error_msg=exc.message)

    # Create a workflow_task row per node, then move the run to 'running'.
    deps_of = dependency_map(dag)
    states: dict[str, NodeState] = {}
    for node in dag.nodes.values():
        row = await repo.create_workflow_task(
            pool, tenant_id=tid, workflow_id=workflow.workflow_id,
            node_id=node.node_id, node_type=node.node_type, description=node.node_id,
            preset=node.preset, depends_on=sorted(deps_of[node.node_id]), retry_max=node.retry_max,
        )
        states[node.node_id] = NodeState(node=node, pk=row.id, version=row.version)
    running = await repo.update_workflow(
        pool, tid, workflow.workflow_id, expected_version=workflow.version, status="running",
        mark_started=True,
    )
    # If the 'running' transition lost the optimistic race (row moved under us — e.g. a cancel),
    # re-read the CURRENT version rather than reusing the guaranteed-stale one (else every terminal
    # update below would silently no-op and leave the run stuck non-terminal).
    wf_version = await _resolve_wf_version(pool, tid, workflow.workflow_id, running, workflow.version)

    blocked: set[str] = set()  # nodes cascade-skipped because their deps were unsatisfied
    # Seed the accumulators with the orchestrator's pre-run spend (decomposition planning), so the
    # budget ceiling + persisted totals include it.
    spent_cost, spent_tokens = initial_cost, initial_tokens
    deadline = _deadline_from(workflow.timeout_at)  # wall-clock run timeout (from timeout_seconds)
    term_status: str | None = None  # 'failed' | 'cancelled' | 'timeout' — set to stop the run early
    err_code: str | None = None
    err_msg: str | None = None

    def _terminate(status: str, code: str | None, msg: str | None) -> None:
        nonlocal term_status, err_code, err_msg
        if term_status is None:
            term_status, err_code, err_msg = status, code, msg

    try:
        for layer in layers:
            if term_status is not None:
                break
            if await _safe_cancel(cancel_check):  # workflow cancel -> stop scheduling
                _terminate("cancelled", ErrorCode.SERVICE_UNAVAILABLE, "Workflow cancelled.")
                break
            if _past_deadline(deadline):  # wall-clock run timeout -> stop scheduling
                _terminate("timeout", ErrorCode.SERVICE_UNAVAILABLE, "Workflow timed out.")
                break

            ready = [nid for nid in layer if deps_satisfied(deps_of[nid], states, blocked)]
            for nid in layer:
                if nid not in ready:  # deps not satisfied -> cascade-skip (and block descendants)
                    blocked.add(nid)
                    await _mark_node(pool, tid, states[nid], status="skipped")

            results = await _run_layer(
                pool=pool, settings=settings, token_provider=token_provider, orchestrator=orchestrator,
                workflow=workflow, states=states, ready=ready, roster=roster, deps_of=deps_of,
                trace_id=trace_id, request_id=request_id, node_budget_seconds=node_budget_seconds,
                node_cost_budget=remaining_cost(cost_budget, spent_cost), cancel_check=cancel_check,
                hil_gate=hil_gate, default_agent_id=default_agent_id,
            )
            for nid, result in results:
                if _node_cancelled(result):  # a user cancel tore this node down -> cancel the run
                    task_id = result.task_id if isinstance(result, SubAgentResult) else None
                    await _mark_node(pool, tid, states[nid], status="cancelled", task_id=task_id)
                    _terminate("cancelled", ErrorCode.SERVICE_UNAVAILABLE, "Workflow cancelled.")
                    continue
                fatal = await _apply_node_result(pool, tid, states[nid], result)
                if isinstance(result, SubAgentResult):
                    spent_cost += result.cost_usd
                    spent_tokens += result.tokens_used
                if fatal is not None:
                    _terminate("failed", fatal[0], fatal[1])
            if term_status is None and over_budget(spent_cost, cost_budget, spent_tokens, token_budget):
                _terminate(
                    "failed", ErrorCode.BUDGET_EXCEEDED,
                    f"Workflow budget exceeded (cost=${spent_cost:.4f}, tokens={spent_tokens}).",
                )
    except Exception as exc:  # noqa: BLE001 — the driver must ALWAYS record a terminal workflow status
        logger.error("workflow_driver_crashed", workflow_id=workflow.workflow_id, error=str(exc))
        _terminate("failed", ErrorCode.INTERNAL_ERROR, f"Orchestration driver error: {exc}")

    summaries = {nid: st.summary for nid, st in states.items()}
    if term_status is not None:  # failed or cancelled
        await _mark_remaining_terminal(pool, tid, states)  # no node left 'pending'/'running'
        await repo.update_workflow(
            pool, tid, workflow.workflow_id, expected_version=wf_version, status=term_status,
            error_code=err_code, error_msg=err_msg, tokens_used=spent_tokens, cost_usd=spent_cost,
            mark_completed=True,
        )
        return WorkflowOutcome(
            status=term_status, output={}, error_code=err_code, error_msg=err_msg,
            tokens_used=spent_tokens, cost_usd=spent_cost, node_summaries=summaries,
        )

    # Success — synthesize the leaf summaries into a final answer (LLM when wired, else leaf-join).
    if synthesizer is not None:
        try:
            answer, s_tokens, s_cost = await synthesizer(workflow.goal, summaries)
            spent_tokens += s_tokens
            spent_cost += s_cost
        except Exception as exc:  # noqa: BLE001 — synthesis is best-effort; fall back to the leaf-join
            logger.warning("workflow_synthesis_failed", workflow_id=workflow.workflow_id, error=str(exc))
            answer = _default_synthesis(dag, summaries)
    else:
        answer = _default_synthesis(dag, summaries)
    output = {"message": answer}
    await repo.update_workflow(
        pool, tid, workflow.workflow_id, expected_version=wf_version, status="completed",
        output=output, tokens_used=spent_tokens, cost_usd=spent_cost, mark_completed=True,
    )
    return WorkflowOutcome(
        status="completed", output=output, tokens_used=spent_tokens, cost_usd=spent_cost,
        node_summaries=summaries,
    )


async def _run_layer(
    *,
    pool: AsyncConnectionPool,
    settings: Settings,
    token_provider: SubAgentTokenProvider,
    orchestrator: Principal,
    workflow: repo.WorkflowRow,
    states: dict[str, NodeState],
    ready: list[str],
    roster: dict[str, str],
    deps_of: dict[str, set[str]],
    trace_id: str,
    request_id: str,
    node_budget_seconds: float,
    node_cost_budget: float | None = None,
    cancel_check: CancelCheck | None = None,
    hil_gate: HilGate | None = None,
    default_agent_id: str | None = None,
) -> list[tuple[str, SubAgentResult | ApiError]]:
    """Run every ready node in a layer CONCURRENTLY; return (node_id, result-or-error) pairs.

    EVERY failure mode is captured as a node-level ``ApiError`` VALUE (never a raised exception) so
    ONE node's infra error (a DB blip, a malformed mint response, a pipeline crash) fails just that
    node — the workflow row is always finalised, and sibling coroutines are never orphaned. Each node
    is capped at ``node_cost_budget`` (the workflow's remaining budget) and shares the run's
    ``cancel_check`` so an in-flight node aborts on a workflow cancel.
    """

    async def _one(nid: str) -> tuple[str, SubAgentResult | ApiError]:
        node = states[nid].node
        try:
            agent_id = resolve_node_agent(node, roster, default_agent_id=default_agent_id)
        except ApiError as exc:  # UNKNOWN_AGENT — the planner named an agent that does not exist
            return nid, exc
        if agent_id is None:  # no agent named AND no default to fall back to
            return nid, ApiError(
                ErrorCode.UNASSIGNED_NODE,
                f"Node {nid!r} names no agent and there is no default agent to run it.",
            )
        # HIL gate (fail-closed): auto-approves under 'automated' mode; otherwise the node waits
        # 'awaiting_approval' for a human verdict. A denial flows through the node's on_error policy.
        #
        # It gates SUB-AGENT CREATION specifically. A node the planner routed to the ORCHESTRATOR
        # ITSELF spawns no sub-agent, so there is nothing here to approve — and now that "delegate to
        # nobody" is a first-class planner outcome, gating it would stop every such run to ask a human
        # to approve a delegation that is not happening (and, under 'ask' mode, fail it when the wait
        # budget elapsed). Gate delegation; do not gate the lead agent doing its own work.
        delegating = agent_id != orchestrator.agent_id
        if hil_gate is not None and delegating:
            await _mark_node(
                pool, orchestrator.tenant_id, states[nid],
                status="awaiting_approval", assigned_agent_id=agent_id,
            )
            verdict = await _await_gate(
                hil_gate,
                cancel_check,
                operation="sub_agent_creation",
                context={
                    "workflow_id": workflow.workflow_id,
                    "node_id": nid,
                    "sub_agent_id": agent_id,
                    "preset": node.preset,
                },
            )
            if verdict is None:  # cancelled while awaiting approval -> run cancellation
                return nid, ApiError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    f"Node {nid!r} cancelled while awaiting approval.",
                    details={"reason": "CANCELLED"},
                )
            if not verdict:
                return nid, ApiError(
                    ErrorCode.FORBIDDEN,
                    f"HIL approval denied for node {nid!r}.",
                    details={"reason": "HIL_DENIED"},
                )
        await _mark_node(
            pool, orchestrator.tenant_id, states[nid], status="running", assigned_agent_id=agent_id
        )
        summaries = {k: v.summary for k, v in states.items()}
        message = render_node_message(node, workflow.goal, summaries, dep_ids=sorted(deps_of.get(nid, set())))

        async def _publish_task_id(task_id: str, _nid: str = nid) -> None:
            """Stamp the child task id on the node AS SOON AS it exists, not at completion.

            The execution tree follows a node's tool calls through this id. Stamping it only on
            completion (the old behaviour) meant a sub-agent's tools were invisible for the whole
            time it was actually calling them. ``mark_started=False`` so the already-set
            ``started_at`` is not pushed forward by this write.
            """
            states[_nid].task_id = task_id
            await _mark_node(
                pool, orchestrator.tenant_id, states[_nid], status="running",
                task_id=task_id, mark_started=False,
            )

        try:
            result = await run_subagent_task(
                pool=pool, settings=settings, token_provider=token_provider, orchestrator=orchestrator,
                sub_agent_id=agent_id, workflow_id=workflow.workflow_id, parent_task_id=None,
                message=message, trace_id=trace_id, request_id=request_id,
                budget_seconds=node_budget_seconds, cost_budget_usd=node_cost_budget,
                # The run-level tool switch reaches EVERY node from the workflow row — the
                # orchestrator answering a step itself included, since it is just another node.
                use_tools=workflow.use_tools,
                cancel_check=cancel_check, on_task_created=_publish_task_id,
            )
            return nid, result
        except ApiError as exc:
            return nid, exc
        except Exception as exc:  # noqa: BLE001 — one node's infra error fails that node, not the run
            logger.error("subagent_node_crashed", node_id=nid, error=str(exc), exc_info=exc)
            return nid, ApiError(ErrorCode.INTERNAL_ERROR, f"Sub-agent node {nid} crashed: {exc}")

    raw = await asyncio.gather(*(_one(nid) for nid in ready), return_exceptions=True)
    out: list[tuple[str, SubAgentResult | ApiError]] = []
    for nid, item in zip(ready, raw, strict=True):
        if isinstance(item, tuple):
            out.append(item)
        else:  # a BaseException escaped _one's own guards (should be unreachable) — coerce to failure
            logger.error("subagent_gather_exception", node_id=nid, error=str(item))
            out.append((nid, ApiError(ErrorCode.INTERNAL_ERROR, f"Sub-agent node {nid} crashed.")))
    return out


async def _apply_node_result(
    pool: AsyncConnectionPool, tenant_id: str, state: NodeState, result: SubAgentResult | ApiError
) -> tuple[str, str] | None:
    """Persist a node's result and decide whether it is a FATAL failure for the workflow.

    Returns ``(error_code, error_msg)`` when the node failed AND its ``on_error`` is ``fail`` (the
    default) — the caller then aborts the run. ``on_error='skip'`` / ``'continue'`` mark the node
    ``skipped`` and return ``None`` (the run continues with partial results).
    """
    if isinstance(result, ApiError):
        return await _on_node_failure(pool, tenant_id, state, result.code, result.message)
    state.task_id = result.task_id
    if result.is_success:
        state.summary = result.summary or ""
        await _mark_node(
            pool, tenant_id, state, status="completed", task_id=result.task_id,
            output=result.to_output(), tokens_used=result.tokens_used, cost_usd=result.cost_usd,
        )
        return None
    return await _on_node_failure(
        pool, tenant_id, state, result.error_code or "SUBAGENT_FAILED",
        result.error_msg or f"Sub-agent node {state.node.node_id} failed.", task_id=result.task_id,
    )


async def _on_node_failure(
    pool: AsyncConnectionPool, tenant_id: str, state: NodeState, code: str, msg: str,
    *, task_id: str | None = None,
) -> tuple[str, str] | None:
    """Apply the node's ``on_error`` policy; return the fatal (code,msg) iff it aborts the run.

    ``fail`` (default) marks the node ``failed`` and returns the fatal reason (the caller aborts).
    ``skip`` and ``continue`` both mark the node ``skipped`` and return ``None`` — the run proceeds
    and downstream nodes still execute with the partial result (an intentional skip is NOT ``blocked``
    in the driver, unlike a cascade-skip from unsatisfied deps).
    """
    if state.node.on_error in ("skip", "continue"):
        await _mark_node(pool, tenant_id, state, status="skipped", task_id=task_id)
        return None
    await _mark_node(pool, tenant_id, state, status="failed", task_id=task_id)
    return code, msg


async def _resolve_wf_version(
    pool: AsyncConnectionPool, tenant_id: str, workflow_id: str,
    running: repo.WorkflowRow | None, fallback: int,
) -> int:
    """The current workflow version after the 'running' transition (re-read if it raced to None)."""
    if running is not None:
        return running.version
    reread = await repo.get_workflow(pool, tenant_id, workflow_id)
    return reread.version if reread is not None else fallback


async def _mark_remaining_terminal(
    pool: AsyncConnectionPool, tenant_id: str, states: dict[str, NodeState]
) -> None:
    """Mark any still-pending/running node ``skipped`` so none is left non-terminal under a failed run."""
    for state in states.values():
        if state.status in ("pending", "running"):
            await _mark_node(pool, tenant_id, state, status="skipped")


async def _mark_node(
    pool: AsyncConnectionPool, tenant_id: str, state: NodeState, *, status: str,
    assigned_agent_id: str | None = None, task_id: str | None = None,
    output: dict[str, Any] | None = None, tokens_used: int | None = None, cost_usd: float | None = None,
    mark_started: bool | None = None,
) -> None:
    """Optimistic-locked node transition; refresh the cached version (best-effort, never raises).

    ``mark_started`` defaults to "this transition is the start" (``status == 'running'``). Pass it
    explicitly as ``False`` to write a field on an ALREADY-running node (the task_id stamp) without
    re-stamping ``started_at`` — otherwise the node's measured duration would silently lose the time
    between the transition and the stamp.
    """
    fields: dict[str, Any] = {"status": status}
    if assigned_agent_id is not None:
        fields["assigned_agent_id"] = assigned_agent_id
    if task_id is not None:
        fields["task_id"] = task_id
    if tokens_used is not None:
        fields["tokens_used"] = tokens_used
    if cost_usd is not None:
        fields["cost_usd"] = cost_usd
    if mark_started is None:
        mark_started = status == "running"
    mark_completed = status in ("completed", "failed", "skipped")
    updated = await repo.update_workflow_task(
        pool, tenant_id, state.pk, expected_version=state.version, output=output,
        mark_started=mark_started, mark_completed=mark_completed, **fields,
    )
    if updated is not None:
        state.version = updated.version
        state.status = updated.status
    else:
        # Version drifted (a concurrent write); reflect intent locally so scheduling still progresses.
        state.status = status


def _default_synthesis(dag: dagmod.Dag, summaries: dict[str, str]) -> str:
    """A non-LLM default synthesis: the summaries of the terminal (leaf) nodes, in order.

    Leaf = a node no other node depends on (never appears as the ``dep`` side of a dependency pair).
    B5 replaces this with an orchestrator LLM synthesis pass.
    """
    depended_on = {dep for dep, _dependent in dag.dependency_pairs()}
    leaves = [nid for nid in dag.nodes if nid not in depended_on]
    chosen = leaves or list(dag.nodes)
    return "\n\n".join(summaries[nid] for nid in chosen if summaries.get(nid)).strip()
