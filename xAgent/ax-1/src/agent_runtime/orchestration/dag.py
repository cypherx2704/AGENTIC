"""Pure DAG model + validation for the orchestration engine (migration 0008).

No DB, no I/O. Parse a ``workflows/dag.schema.json`` document (the ``xagent.workflows``
``subtask_dag`` JSONB) into typed structures, validate it, and expose the topological
execution *layers* the driver fans out (each layer runs concurrently; layer N depends only
on layers < N).

Validation is deliberately strict and total so a malformed / cyclic decomposition fails
CLOSED before any sub-agent is spawned:
  * every node has a non-empty, unique ``node_id`` and a known ``node_type``;
  * every edge / ``depends_on`` references a known node;
  * the graph is ACYCLIC (Kahn's algorithm — the decomposer, esp. the LLM path, can emit
    cycles); a cycle -> :class:`ApiError` ``INVALID_DAG``;
  * caps: number of layers <= ``max_depth`` (longest dependency chain), and the width of
    any single layer <= ``max_fanout`` (max concurrent sub-agents).

Kept pure so it is exhaustively unit-testable; the driver (B2) only wraps I/O around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ApiError, ErrorCode

#: Default caps (a DAG may lower — never silently raise — them via ``constraints``).
DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_FANOUT = 8

#: Node kinds accepted in a DAG (mirrors workflows/dag.schema.json node_type enum).
VALID_NODE_TYPES = frozenset(
    {"task", "agent", "tool", "skill", "approval", "condition", "fanout", "join", "human"}
)
VALID_ON_ERROR = frozenset({"fail", "skip", "continue"})


@dataclass(frozen=True)
class DagNode:
    """One subtask node (immutable once parsed)."""

    node_id: str
    node_type: str
    ref: str | None = None
    assigned_agent_id: str | None = None
    preset: str | None = None
    input_bindings: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    retry_max: int = 1
    on_error: str = "fail"
    #: Node-level dependencies (in addition to any graph edges) — upstream node_ids.
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class DagEdge:
    """A directed dependency edge: ``to_node`` depends on ``from_node``."""

    from_node: str
    to_node: str
    condition: str | None = None


@dataclass
class Dag:
    """A parsed, not-yet-validated workflow graph."""

    nodes: dict[str, DagNode]
    edges: list[DagEdge]
    max_depth: int = DEFAULT_MAX_DEPTH
    max_fanout: int = DEFAULT_MAX_FANOUT
    max_cost_usd: float | None = None
    max_tokens: int | None = None

    def dependency_pairs(self) -> set[tuple[str, str]]:
        """Return the de-duplicated ``(dependency, dependent)`` pairs (edges + depends_on).

        An edge ``from -> to`` means ``to`` depends on ``from``; a node's ``depends_on``
        entry ``dep`` means the node depends on ``dep``. De-duplicated so a repeated edge
        never double-counts in the in-degree computation.
        """
        pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            pairs.add((edge.from_node, edge.to_node))
        for node in self.nodes.values():
            for dep in node.depends_on:
                pairs.add((dep, node.node_id))
        return pairs


def _as_int(value: Any, default: int | None = None) -> int | None:
    """Best-effort int coercion (bool excluded); returns ``default`` on non-numeric input."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _str_or_none(value: Any) -> str | None:
    """Return ``value`` if it is a string, else ``None`` (tolerant field coercion)."""
    return value if isinstance(value, str) else None


def parse_dag(doc: dict[str, Any]) -> Dag:
    """Parse a ``subtask_dag`` document into a :class:`Dag` (structure only — validate next).

    Raises :class:`ApiError` ``INVALID_DAG`` for structural problems that make the document
    un-parseable as a graph: a non-object document, no nodes, a node without an id, a
    duplicate node_id, or an unknown ``node_type`` / ``on_error``.
    """
    if not isinstance(doc, dict):
        # Fail CLOSED (422 INVALID_DAG) — subtask_dag is a nullable JSONB column, so a driver
        # can hand us SQL NULL / a JSON array / a scalar; never let that become a 500.
        raise ApiError(ErrorCode.INVALID_DAG, "Workflow DAG document must be an object.")
    raw_nodes = doc.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ApiError(ErrorCode.INVALID_DAG, "Workflow DAG has no nodes.")

    nodes: dict[str, DagNode] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ApiError(ErrorCode.INVALID_DAG, "Each DAG node must be an object.")
        node_id = raw.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ApiError(ErrorCode.INVALID_DAG, "Every DAG node needs a non-empty node_id.")
        if node_id in nodes:
            raise ApiError(ErrorCode.INVALID_DAG, f"Duplicate node_id in DAG: {node_id!r}.")
        node_type = raw.get("node_type")
        if node_type not in VALID_NODE_TYPES:
            raise ApiError(ErrorCode.INVALID_DAG, f"Unknown node_type {node_type!r} for node {node_id!r}.")
        on_error = raw.get("on_error", "fail")
        if on_error not in VALID_ON_ERROR:
            raise ApiError(ErrorCode.INVALID_DAG, f"Unknown on_error {on_error!r} for node {node_id!r}.")
        retry = raw.get("retry")
        # NB: preserve an explicit retry.max of 0 (no retries) — do NOT `or 1` it away.
        retry_max = _as_int(retry.get("max") if isinstance(retry, dict) else None, 1)
        raw_depends = raw.get("depends_on")
        depends_on = (
            tuple(d for d in raw_depends if isinstance(d, str) and d.strip())
            if isinstance(raw_depends, list)
            else ()
        )
        bindings = raw.get("input_bindings")
        nodes[node_id] = DagNode(
            node_id=node_id,
            node_type=node_type,
            ref=_str_or_none(raw.get("ref")),
            assigned_agent_id=_str_or_none(raw.get("assigned_agent_id")),
            preset=_str_or_none(raw.get("preset")),
            input_bindings=bindings if isinstance(bindings, dict) else {},
            timeout_seconds=_as_int(raw.get("timeout_seconds")),
            retry_max=retry_max if retry_max is not None else 1,
            on_error=on_error,
            depends_on=depends_on,
        )

    edges: list[DagEdge] = []
    raw_edges = doc.get("edges") or []
    if isinstance(raw_edges, list):
        for raw in raw_edges:
            if not isinstance(raw, dict):
                raise ApiError(ErrorCode.INVALID_DAG, "Each DAG edge must be an object.")
            from_node = raw.get("from_node")
            to_node = raw.get("to_node")
            if not isinstance(from_node, str) or not isinstance(to_node, str) or not from_node or not to_node:
                raise ApiError(ErrorCode.INVALID_DAG, "Every DAG edge needs from_node and to_node.")
            edges.append(
                DagEdge(
                    from_node=from_node,
                    to_node=to_node,
                    condition=raw.get("condition") if isinstance(raw.get("condition"), str) else None,
                )
            )

    raw_constraints = doc.get("constraints")
    constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
    max_depth = _as_int(constraints.get("max_depth"), DEFAULT_MAX_DEPTH) or DEFAULT_MAX_DEPTH
    max_fanout = _as_int(constraints.get("max_fanout"), DEFAULT_MAX_FANOUT) or DEFAULT_MAX_FANOUT
    return Dag(
        nodes=nodes,
        edges=edges,
        # The DEFAULT_* caps are a HARD CEILING a DAG can only LOWER — never silently raise.
        # A DAG-supplied (esp. LLM-emitted / prompt-influenced) constraints block must not be
        # able to inflate the anti-runaway fanout/depth bound.
        max_depth=min(DEFAULT_MAX_DEPTH, max(1, max_depth)),
        max_fanout=min(DEFAULT_MAX_FANOUT, max(1, max_fanout)),
        max_cost_usd=_as_float(constraints.get("max_cost_usd")),
        max_tokens=_as_int(constraints.get("max_tokens")),
    )


def topological_layers(dag: Dag) -> list[list[str]]:
    """Return the DAG's execution layers via Kahn's algorithm (each layer sorted, stable).

    Layer 0 = all in-degree-0 nodes (no dependencies); layer N = nodes whose dependencies
    all resolved by layers < N. Raises :class:`ApiError` ``INVALID_DAG`` on a cycle (some
    node never reaches in-degree 0) — this is the mandatory pre-execution cycle check.

    Assumes edges/``depends_on`` reference known nodes; call :func:`validate_dag` (which
    checks that first) rather than this directly for untrusted input.
    """
    indeg: dict[str, int] = dict.fromkeys(dag.nodes, 0)
    adj: dict[str, list[str]] = {node_id: [] for node_id in dag.nodes}
    for dep, dependent in dag.dependency_pairs():
        # Only count pairs whose endpoints are real nodes (validate_dag enforces this; guard
        # here too so a direct call cannot KeyError).
        if dep in indeg and dependent in indeg:
            adj[dep].append(dependent)
            indeg[dependent] += 1

    frontier = sorted(node_id for node_id, deg in indeg.items() if deg == 0)
    layers: list[list[str]] = []
    resolved = 0
    while frontier:
        layers.append(frontier)
        resolved += len(frontier)
        nxt: list[str] = []
        for node_id in frontier:
            for dependent in adj[node_id]:
                indeg[dependent] -= 1
                if indeg[dependent] == 0:
                    nxt.append(dependent)
        frontier = sorted(nxt)

    if resolved != len(dag.nodes):
        raise ApiError(ErrorCode.INVALID_DAG, "Workflow DAG contains a dependency cycle.")
    return layers


def validate_dag(dag: Dag) -> list[list[str]]:
    """Validate ``dag`` fully and return its topological layers (the driver's schedule).

    Checks edge/``depends_on`` referential integrity, acyclicity (Kahn), and the depth /
    fanout caps. Raises :class:`ApiError` ``INVALID_DAG`` on any violation — the run should
    then fail with ``error_code = INVALID_DAG`` and spawn NO sub-agents.
    """
    for dep, dependent in dag.dependency_pairs():
        if dep not in dag.nodes:
            raise ApiError(ErrorCode.INVALID_DAG, f"Dependency references unknown node: {dep!r}.")
        if dependent not in dag.nodes:
            raise ApiError(ErrorCode.INVALID_DAG, f"Dependency references unknown node: {dependent!r}.")

    layers = topological_layers(dag)

    if len(layers) > dag.max_depth:
        raise ApiError(
            ErrorCode.INVALID_DAG,
            f"Workflow depth {len(layers)} exceeds max_depth {dag.max_depth}.",
            details={"depth": len(layers), "max_depth": dag.max_depth},
        )
    widest = max((len(layer) for layer in layers), default=0)
    if widest > dag.max_fanout:
        raise ApiError(
            ErrorCode.INVALID_DAG,
            f"Workflow fanout {widest} exceeds max_fanout {dag.max_fanout}.",
            details={"fanout": widest, "max_fanout": dag.max_fanout},
        )
    return layers
