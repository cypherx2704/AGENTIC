"""Goal decomposition — THE PLANNER DECIDES; this module only validates.

Turns a natural-language goal into a ``subtask_dag`` document (workflows/dag.schema.json shape) for
the driver to execute. Exactly one thing produces that graph: the orchestrator LLM (the injected
:data:`Planner`). This module never *chooses* an agent, never *invents* a step, and never
*substitutes* one target for another. Its entire job is:

  1. hand the goal to the planner;
  2. translate the plan MECHANICALLY into a DAG (:func:`plan_to_dag` — no inference, no defaults);
  3. VALIDATE it — acyclic, inside the depth/fanout caps (:mod:`.dag`), and every target a real
     roster entry (:func:`validate_targets`); and
  4. when validation fails, hand it BACK to the planner once, with the reason.

**There is deliberately no keyword router and no built-in "research → write → review" template.**
Both used to live here and both were routing rules in disguise: a goal containing the substring
"compare" was fanned out to three ``researcher`` sub-agents by an ``if``, with the LLM never
consulted. Substring matching also cannot read a negation — "…and do NOT write a brief" matched the
'write' keyword and produced a brief-writing step anyway. The only template left is ``solo``: a
SINGLE node run by the orchestrator itself. That is the *no-delegation* graph, not a choice of
sub-agent.

**Failure is loud, never silent.** An unusable plan does not quietly degrade onto some default
sub-agent. It goes back to the planner once (gated by :data:`RetryApprover`, so a human can be told
first) and, failing that, raises ``ORCHESTRATION_FAILED`` and the run ends. A backend that quietly
picks a substitute has overridden the model's decision — the one thing this module must never do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from ..core.errors import ApiError, ErrorCode
from . import dag as dagmod

logger = structlog.get_logger(__name__)

#: An injected LLM decomposer: ``(goal, feedback) -> plan document``, where a plan is the a2a
#: ``plan`` task-type shape ``{"steps": [{step, depends_on, id?, preset?, task_type?}]}``.
#: ``feedback`` is ``None`` on the first attempt and carries the rejection reason on the single
#: permitted repair attempt. ``None`` (no planner at all) disables the LLM path entirely.
Planner = Callable[[str, str | None], Awaitable[dict[str, Any]]]

#: Asked for permission to RE-PLAN after the planner produced an unusable plan. Receives the
#: human-readable rejection reason; returns True to let the planner try again, False to fail the run.
#: :mod:`.service` wires this to the HIL approval gate — an explicit human DENY maps to False
#: (hard-fail), while a GRANT *or* an unreachable HIL maps to True (retry anyway, rather than
#: stranding a run because nobody was around to answer). ``None`` = retry without asking.
RetryApprover = Callable[[str], Awaitable[bool]]

#: The name of the only surviving template: one node, run by the orchestrator, delegating to nobody.
SOLO_TEMPLATE = "solo"

#: One plan, plus one repair. A second rejection fails the run — an LLM that cannot satisfy an
#: explicit, itemised rejection twice will not be talked into it by a third try, and each attempt is
#: a real planning call the user pays for.
_MAX_PLAN_ATTEMPTS = 2


@dataclass(frozen=True)
class Decomposition:
    """The result of decomposing a goal."""

    #: A workflows/dag.schema.json document (goes into ``xagent.workflows.subtask_dag``).
    dag_doc: dict[str, Any]
    #: How it was produced — ``llm`` (the planner) or ``template`` (the ``solo`` no-delegation graph).
    decomposition: str
    #: ``solo`` when ``decomposition == 'template'``; else ``None``.
    template: str | None = None


def _str_or(value: Any, default: str) -> str:
    """Return ``value`` if it is a string, else ``default`` (tolerant field coercion)."""
    return value if isinstance(value, str) else default


def _opt_str(value: Any) -> str | None:
    """Return ``value`` if it is a string, else ``None``."""
    return value if isinstance(value, str) else None


# ── graph construction ───────────────────────────────────────────────────────────────────
def _node(node_id: str, *, preset: str | None = None, ref: str, node_type: str = "agent",
          description: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {"node_id": node_id, "node_type": node_type, "ref": ref}
    if preset is not None:
        node["preset"] = preset
    if description:
        node["description"] = description
    return node


def _envelope(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    goal: str,
    workflow_id: str,
    tenant_id: str,
    name: str,
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "name": name,
        "goal": goal,
        "nodes": nodes,
        "edges": edges,
    }


def build_solo_dag(*, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    """The NO-DELEGATION graph: one node, run by the orchestrator itself.

    The node deliberately carries **no ``preset``**. The driver binds a preset-less node to
    ``default_agent_id``, which is the ORCHESTRATOR — so this is the one and only place a node is
    bound without the planner having named a target, and it binds to the lead agent, never to a
    sub-agent the planner did not pick.
    """
    return _envelope(
        [_node("main", node_type="task", ref="chat", description=goal)],
        [],
        goal=goal, workflow_id=workflow_id, tenant_id=tenant_id, name=SOLO_TEMPLATE,
    )


def plan_to_dag(plan: dict[str, Any], *, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    """Convert an LLM ``plan`` output into a ``subtask_dag`` document — a MECHANICAL translation.

    Each step becomes an ``agent`` node; each ``depends_on`` entry becomes an edge. Nothing is
    inferred: a step's target is whatever the planner wrote in ``preset`` (or nothing at all, which
    :func:`validate_targets` then rejects). Raises ``INVALID_DAG`` when there are no usable steps.
    """
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ApiError(ErrorCode.INVALID_DAG, "The plan contained no steps.")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    ids: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ApiError(ErrorCode.INVALID_DAG, "Each plan step must be a JSON object.")
        raw_id = step.get("id")
        node_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else f"step-{i + 1}"
        ids.append(node_id)
        nodes.append(
            _node(
                node_id,
                preset=_opt_str(step.get("preset")),
                ref=_str_or(step.get("task_type"), "generate"),
                description=_str_or(step.get("step"), ""),
            )
        )

    for i, step in enumerate(steps):
        raw_deps = step.get("depends_on") if isinstance(step, dict) else None
        if isinstance(raw_deps, list):
            for dep in raw_deps:
                if isinstance(dep, str) and dep.strip():
                    edges.append({"from_node": dep, "to_node": ids[i]})

    return _envelope(nodes, edges, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id,
                     name="llm-plan")


# ── validation (never routing) ───────────────────────────────────────────────────────────
def _target_list(targets: Iterable[str]) -> str:
    return ", ".join(sorted(targets)) or "(none)"


def validate_targets(dag_doc: dict[str, Any], targets: Sequence[str] | None) -> None:
    """Assert every node names a target that actually EXISTS. ``targets=None`` skips (roster unknown).

    This is the guard that stops a hallucinated or mistyped agent name from being silently re-routed
    onto a default. Note what it does NOT do: it never picks a replacement. It only reports that the
    planner named something that was not on the menu, so the planner can be asked to fix it. Choosing
    the agent stays the planner's job, including when the planner gets it wrong.

    A step with NO target is rejected for the same reason: the prompt requires one on every step, so
    an absent ``preset`` is a malformed plan, not an invitation for the backend to pick.
    """
    if targets is None:
        return
    valid = set(targets)
    for node in dag_doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = node.get("node_id", "?")
        preset = node.get("preset")
        if not isinstance(preset, str) or not preset.strip():
            raise ApiError(
                ErrorCode.INVALID_DAG,
                f"Step {node_id!r} names no target agent. Every step must name exactly one of: "
                f"{_target_list(valid)}.",
            )
        if preset not in valid:
            raise ApiError(
                ErrorCode.UNKNOWN_AGENT,
                f"Step {node_id!r} targets {preset!r}, which is not an available agent. "
                f"Valid targets: {_target_list(valid)}.",
                details={"step": node_id, "requested_agent": preset, "known_agents": sorted(valid)},
            )


def repair_feedback(reason: str, targets: Sequence[str] | None) -> str:
    """The user turn appended on the repair attempt: what was wrong, and what is actually allowed."""
    lines = [f"Your previous plan was REJECTED and was NOT executed. Reason: {reason}"]
    if targets is not None:
        lines.append(f'The ONLY valid "preset" values are: {_target_list(targets)}.')
    lines.append(
        "Re-plan so that it satisfies the rules. Prefer FEWER steps — one step targeting "
        '"orchestrator" is a perfectly good plan when no sub-agent is needed. '
        "Reply with ONLY the JSON object."
    )
    return "\n".join(lines)


def _reason(exc: Exception) -> str:
    """A one-line, model-readable statement of why a plan was rejected."""
    if isinstance(exc, ApiError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


async def _may_retry(approve_retry: RetryApprover | None, reason: str) -> bool:
    """Ask whether the planner may try again. No approver — or a broken one — means yes.

    The gate exists so a human is TOLD the plan failed before another planning call is spent, and can
    stop the run there. But an approval channel that cannot be reached is not a refusal: only an
    explicit "no" stops the retry (see :mod:`.service`, which maps a HIL deny to False and an
    unreachable HIL to True).
    """
    if approve_retry is None:
        return True
    try:
        return await approve_retry(reason)
    except Exception as exc:  # noqa: BLE001 — an approval-channel fault is not a human's refusal
        logger.warning("orchestration_retry_approval_errored", error=str(exc))
        return True


# ── the public entrypoint ────────────────────────────────────────────────────────────────
async def decompose(
    goal: str,
    *,
    workflow_id: str,
    tenant_id: str,
    mode: str = "subagents",
    planner: Planner | None = None,
    targets: Sequence[str] | None = None,
    approve_retry: RetryApprover | None = None,
) -> Decomposition:
    """Decompose ``goal`` into a validated ``subtask_dag``. The planner decides; we only validate.

    Order:
      1. ``mode == 'solo'`` (the CALLER explicitly opted out of delegation) or a blank goal → the
         single-node ``solo`` graph. No planner call. This is a user's choice, not the backend's.
      2. no ``planner`` → the ``solo`` graph. With no model there is nobody to make a routing
         decision, and the backend is not permitted to make one on its behalf — so it delegates to
         nobody rather than guessing at an agent.
      3. otherwise → the planner plans. Its plan is translated and validated (cycle / depth / fanout
         via :mod:`.dag`, then :func:`validate_targets` against ``targets`` — the live roster plus
         ``orchestrator``). A rejected plan goes back to the planner ONCE with the reason, after
         ``approve_retry`` permits it.

    Args:
        targets: every valid target name (the roster's sub-agents + ``orchestrator``). ``None``
            means "roster unknown" and skips target validation.

    Raises:
        ApiError: ``ORCHESTRATION_FAILED`` when the planner could not produce a usable plan — the
            retry was declined, or it also failed. NO sub-agent is spawned and the run fails, rather
            than the backend substituting an agent the planner never chose.
    """
    if mode == "solo" or not goal.strip():
        return Decomposition(
            dag_doc=build_solo_dag(goal=goal, workflow_id=workflow_id, tenant_id=tenant_id),
            decomposition="template",
            template=SOLO_TEMPLATE,
        )

    if planner is None:
        logger.info("orchestration_no_planner_no_delegation", workflow_id=workflow_id)
        return Decomposition(
            dag_doc=build_solo_dag(goal=goal, workflow_id=workflow_id, tenant_id=tenant_id),
            decomposition="template",
            template=SOLO_TEMPLATE,
        )

    feedback: str | None = None
    for attempt in range(1, _MAX_PLAN_ATTEMPTS + 1):
        try:
            plan = await planner(goal, feedback)
            dag_doc = plan_to_dag(plan, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id)
            dagmod.validate_dag(dagmod.parse_dag(dag_doc))  # cycle + depth/fanout caps
            validate_targets(dag_doc, targets)              # every step names a REAL agent
        except Exception as exc:  # noqa: BLE001 — ANY unusable plan takes the repair path
            reason = _reason(exc)
            logger.warning(
                "orchestration_plan_rejected",
                workflow_id=workflow_id, attempt=attempt, reason=reason,
            )
            if attempt >= _MAX_PLAN_ATTEMPTS:
                raise ApiError(
                    ErrorCode.ORCHESTRATION_FAILED,
                    "Agent orchestration failed: the planner could not produce a valid plan after "
                    f"a retry. {reason}",
                    details={"reason": reason, "attempts": attempt},
                ) from exc
            if not await _may_retry(approve_retry, reason):
                raise ApiError(
                    ErrorCode.ORCHESTRATION_FAILED,
                    "Agent orchestration failed: the plan was invalid and permission to re-plan was "
                    f"declined. {reason}",
                    details={"reason": reason, "declined": True},
                ) from exc
            feedback = repair_feedback(reason, targets)
            continue
        return Decomposition(dag_doc=dag_doc, decomposition="llm", template=None)

    # Unreachable: the loop either returns a plan or raises. Kept so the function is total.
    raise ApiError(ErrorCode.ORCHESTRATION_FAILED, "Agent orchestration failed: no plan was produced.")
