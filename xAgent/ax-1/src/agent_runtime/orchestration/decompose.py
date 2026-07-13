"""Goal decomposition for the orchestration engine (phase B1) — deterministic-first.

Turns a natural-language goal into a ``subtask_dag`` document (workflows/dag.schema.json shape)
that the driver executes. The cost lever (per SUBAGENT_WORKFLOW_PLAN.md §7 #2) is: match the goal
to a DETERMINISTIC TEMPLATE and skip the planning-LLM call entirely; fall back to an LLM ``plan``
decomposition only for goals no template matches; and if no planner is available, fall back to a
safe single-node ("solo") DAG rather than failing.

Pure + injectable: the LLM path takes a ``planner`` callable, so this whole module is unit-tested
with no network. The produced DAG is always validated (Kahn cycle check + the hard depth/fanout
ceiling) via :mod:`.dag` before it is returned — a malformed decomposition fails CLOSED
(``INVALID_DAG``) and spawns nothing.

Node -> concrete sub-agent binding (``preset`` -> a specific sub-agent) is deliberately left to the
driver (B2): templates emit ``preset`` names (researcher/writer/reviewer, the seeded set) so the same
DAG shape works for any tenant's roster.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from ..core.errors import ApiError, ErrorCode
from . import dag as dagmod

logger = structlog.get_logger(__name__)

#: An injected LLM decomposer: goal -> a ``plan`` document ``{"steps": [{step, depends_on, ...}]}``
#: (the a2a ``plan`` task-type output shape). ``None`` disables the LLM path.
Planner = Callable[[str], Awaitable[dict[str, Any]]]

#: Fixed fan-out width for the parallel-research template (deterministic; the LLM path may vary it).
PARALLEL_RESEARCH_BRANCHES = 3

# Preset names templates emit (the seeded researcher/writer/reviewer bundles).
_RESEARCHER = "researcher"
_WRITER = "writer"
_REVIEWER = "reviewer"


@dataclass(frozen=True)
class Decomposition:
    """The result of decomposing a goal."""

    #: A workflows/dag.schema.json document (goes into ``xagent.workflows.subtask_dag``).
    dag_doc: dict[str, Any]
    #: How it was produced — ``template`` (deterministic) or ``llm`` (planner call).
    decomposition: str
    #: The template name when ``decomposition == 'template'`` (e.g. ``solo``); else ``None``.
    template: str | None = None


def _str_or(value: Any, default: str) -> str:
    """Return ``value`` if it is a string, else ``default`` (tolerant field coercion)."""
    return value if isinstance(value, str) else default


def _opt_str(value: Any) -> str | None:
    """Return ``value`` if it is a string, else ``None``."""
    return value if isinstance(value, str) else None


# ── template builders ────────────────────────────────────────────────────────────────────
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
    max_fanout: int | None = None,
) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    if max_fanout is not None:
        constraints["max_fanout"] = max_fanout
    doc: dict[str, Any] = {
        "workflow_id": workflow_id,
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "name": name,
        "goal": goal,
        "nodes": nodes,
        "edges": edges,
    }
    if constraints:
        doc["constraints"] = constraints
    return doc


def _tpl_solo(*, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    """A single node run by the orchestrator itself (no sub-agents) — the safe default."""
    return _envelope(
        [_node("main", node_type="task", ref="chat", description=goal)],
        [],
        goal=goal, workflow_id=workflow_id, tenant_id=tenant_id, name="solo",
    )


def _tpl_research_write(*, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    nodes = [
        _node("research", preset=_RESEARCHER, ref="research", description="Gather sources + findings."),
        _node("write", preset=_WRITER, ref="generate", description="Draft the answer from findings."),
    ]
    edges = [{"from_node": "research", "to_node": "write"}]
    return _envelope(nodes, edges, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id,
                     name="research-write")


def _tpl_research_write_review(*, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    nodes = [
        _node("research", preset=_RESEARCHER, ref="research", description="Gather sources + findings."),
        _node("write", preset=_WRITER, ref="generate", description="Draft the answer from findings."),
        _node("review", preset=_REVIEWER, ref="code-review", description="Review + refine the draft."),
    ]
    edges = [
        {"from_node": "research", "to_node": "write"},
        {"from_node": "write", "to_node": "review"},
    ]
    return _envelope(nodes, edges, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id,
                     name="research-write-review")


def _tpl_parallel_research(*, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    branches = [f"research-{i + 1}" for i in range(PARALLEL_RESEARCH_BRANCHES)]
    nodes = [
        _node(b, preset=_RESEARCHER, ref="research", description=f"Investigate angle {i + 1}.")
        for i, b in enumerate(branches)
    ]
    nodes.append(_node("synthesis", preset=_WRITER, ref="generate", description="Synthesize the branches."))
    edges = [{"from_node": b, "to_node": "synthesis"} for b in branches]
    return _envelope(nodes, edges, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id,
                     name="parallel-research", max_fanout=PARALLEL_RESEARCH_BRANCHES)


#: Template registry: name -> builder. ``solo`` is the safe default and never matched by heuristic.
TEMPLATES: dict[str, Callable[..., dict[str, Any]]] = {
    "solo": _tpl_solo,
    "research-write": _tpl_research_write,
    "research-write-review": _tpl_research_write_review,
    "parallel-research": _tpl_parallel_research,
}


# ── deterministic router ─────────────────────────────────────────────────────────────────
_REVIEW_WORDS = ("review", "audit", "critique", "refine", "proofread")
_WRITE_WORDS = ("write", "draft", "report", "summariz", "summaris", "brief", "compose", "essay", "article")
_RESEARCH_WORDS = ("research", "investigate", "find", "gather", "look up", "explore", "analyz", "analys")
_PARALLEL_WORDS = ("compare", "survey", "across", "multiple", "several", "each of", "versus", " vs ")


def match_template(goal: str) -> str | None:
    """Deterministically map ``goal`` to a template name, or ``None`` for the LLM/default path.

    Intentionally conservative keyword heuristics — a miss (``None``) is the signal to try the LLM
    planner, not a failure. Never returns ``solo`` (that is the explicit fallback, not a match).
    """
    g = f" {goal.lower()} "
    has_research = any(w in g for w in _RESEARCH_WORDS)
    has_write = any(w in g for w in _WRITE_WORDS)
    has_review = any(w in g for w in _REVIEW_WORDS)
    has_parallel = any(w in g for w in _PARALLEL_WORDS)

    if has_parallel and (has_research or has_write):
        return "parallel-research"
    if has_write and has_review:
        return "research-write-review"
    if has_research and has_write:
        return "research-write"
    return None


def build_template_dag(name: str, *, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    """Build a template's DAG document by name. ``KeyError`` for an unknown template name."""
    return TEMPLATES[name](goal=goal, workflow_id=workflow_id, tenant_id=tenant_id)


# ── LLM plan -> DAG ──────────────────────────────────────────────────────────────────────
def plan_to_dag(plan: dict[str, Any], *, goal: str, workflow_id: str, tenant_id: str) -> dict[str, Any]:
    """Convert an LLM ``plan`` output into a ``subtask_dag`` document.

    Accepts the a2a ``plan`` task-type shape ``{"steps": [{step, depends_on, id?, preset?, task_type?}]}``.
    Each step becomes an ``agent`` node; ``depends_on`` (referencing other step ids) becomes edges.
    Raises :class:`ApiError` ``INVALID_DAG`` when there are no usable steps (fail closed).
    """
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ApiError(ErrorCode.INVALID_DAG, "LLM plan produced no steps.")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    ids: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ApiError(ErrorCode.INVALID_DAG, "Each plan step must be an object.")
        raw_id = step.get("id")
        node_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else f"step-{i + 1}"
        ids.append(node_id)
        preset = _opt_str(step.get("preset"))
        ref = _str_or(step.get("task_type"), "generate")
        desc = _str_or(step.get("step"), "")
        nodes.append(_node(node_id, preset=preset, ref=ref, description=desc))

    for i, step in enumerate(steps):
        raw_deps = step.get("depends_on") if isinstance(step, dict) else None
        if isinstance(raw_deps, list):
            for dep in raw_deps:
                if isinstance(dep, str) and dep.strip():
                    edges.append({"from_node": dep, "to_node": ids[i]})

    return _envelope(nodes, edges, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id, name="llm-plan")


# ── the public entrypoint ────────────────────────────────────────────────────────────────
async def decompose(
    goal: str,
    *,
    workflow_id: str,
    tenant_id: str,
    mode: str = "subagents",
    template: str | None = None,
    planner: Planner | None = None,
) -> Decomposition:
    """Decompose ``goal`` into a validated ``subtask_dag`` (deterministic-first, LLM fallback).

    Order:
      1. ``mode == 'solo'`` (or a blank goal) -> the single-node ``solo`` template.
      2. an explicit ``template`` name -> that template.
      3. a deterministic :func:`match_template` hit -> that template.
      4. a ``planner`` -> LLM ``plan`` decomposition (``decomposition = 'llm'``); on planner error or
         an empty/invalid plan, fall back to ``solo`` (never fail the run on the planning step).
      5. no planner -> the ``solo`` template (safe default).

    The chosen DAG is always validated (:func:`dag.validate_dag`) — a cyclic / over-cap / malformed
    decomposition raises :class:`ApiError` ``INVALID_DAG`` and no sub-agent is spawned.
    """
    chosen_template: str | None = None
    decomposition = "template"

    if mode == "solo" or not goal.strip():
        chosen_template = "solo"
    elif template is not None:
        if template not in TEMPLATES:
            raise ApiError(ErrorCode.VALIDATION_ERROR, f"Unknown decomposition template: {template!r}.")
        chosen_template = template
    else:
        matched = match_template(goal)
        if matched is not None:
            chosen_template = matched

    if chosen_template is not None:
        dag_doc = build_template_dag(chosen_template, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id)
    elif planner is not None:
        dag_doc, decomposition, chosen_template = await _plan_or_solo(
            goal, workflow_id=workflow_id, tenant_id=tenant_id, planner=planner
        )
    else:
        chosen_template = "solo"
        dag_doc = build_template_dag("solo", goal=goal, workflow_id=workflow_id, tenant_id=tenant_id)

    # Always validate before returning — fail CLOSED on a cyclic / over-cap / malformed graph.
    dagmod.validate_dag(dagmod.parse_dag(dag_doc))
    return Decomposition(dag_doc=dag_doc, decomposition=decomposition, template=chosen_template)


async def _plan_or_solo(
    goal: str, *, workflow_id: str, tenant_id: str, planner: Planner
) -> tuple[dict[str, Any], str, str | None]:
    """Run the LLM planner and validate its DAG; on ANY failure fall back to the solo template.

    The planning step must never fail the whole run: a planner exception, an empty/invalid plan,
    OR a plan that builds into a cyclic / over-cap DAG all degrade to the safe ``solo`` template
    (logged). A genuinely bad graph is caught HERE (validate inside the try) rather than surfacing
    as ``INVALID_DAG`` from the outer :func:`decompose` validation.
    """
    try:
        plan = await planner(goal)
        dag_doc = plan_to_dag(plan, goal=goal, workflow_id=workflow_id, tenant_id=tenant_id)
        dagmod.validate_dag(dagmod.parse_dag(dag_doc))
        return dag_doc, "llm", None
    except Exception as exc:  # noqa: BLE001 — a bad/failed plan degrades to solo; never fails the run
        logger.warning("llm_decomposition_degraded_to_solo", error=str(exc))
        return (
            build_template_dag("solo", goal=goal, workflow_id=workflow_id, tenant_id=tenant_id),
            "template",
            "solo",
        )
