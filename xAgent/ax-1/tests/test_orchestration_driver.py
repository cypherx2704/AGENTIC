"""Unit tests for the DAG driver's pure scheduling/binding helpers (phase B2c).

The integration loop (:func:`driver.run_workflow`) needs the DB + executor + pipeline and is
covered by the review + a future integration test; here we lock down the deterministic units:
node -> sub-agent binding, input-binding message rendering, the full dependency map (edges AND
node-level depends_on), readiness gating, and the leaf-based default synthesis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.orchestration.dag import DagNode, parse_dag
from agent_runtime.orchestration.driver import (
    NodeState,
    _deadline_from,
    _default_synthesis,
    _node_cancelled,
    _past_deadline,
    dependency_map,
    deps_satisfied,
    over_budget,
    remaining_cost,
    render_node_message,
    resolve_node_agent,
)
from agent_runtime.orchestration.executor import SubAgentResult


def _node(**kw: object) -> DagNode:
    kw.setdefault("node_id", "n")
    kw.setdefault("node_type", "agent")
    return DagNode(**kw)  # type: ignore[arg-type]


def _state(status: str) -> NodeState:
    return NodeState(node=_node(), pk="pk", version=1, status=status)


# ── resolve_node_agent ───────────────────────────────────────────────────────────────────
def test_assigned_agent_wins() -> None:
    node = _node(assigned_agent_id="A", preset="researcher")
    assert resolve_node_agent(node, {"researcher": "R"}) == "A"


def test_preset_maps_via_roster() -> None:
    assert resolve_node_agent(_node(preset="researcher"), {"researcher": "R"}) == "R"


def test_falls_back_to_default() -> None:
    assert resolve_node_agent(_node(preset="unknown"), {}, default_agent_id="D") == "D"


def test_unassignable_is_none() -> None:
    assert resolve_node_agent(_node(), {}) is None


# ── render_node_message ──────────────────────────────────────────────────────────────────
def test_no_bindings_is_just_goal() -> None:
    assert render_node_message(_node(), "the goal", {}) == "the goal"


def test_bindings_substitute_upstream_summary() -> None:
    node = _node(input_bindings={"context": "{{research.output}}"})
    msg = render_node_message(node, "write it up", {"research": "the findings"})
    assert msg == "write it up\n\ncontext:\nthe findings"


def test_missing_summary_renders_empty_and_is_dropped() -> None:
    node = _node(input_bindings={"context": "{{missing.output}}"})
    assert render_node_message(node, "goal", {}) == "goal"


def test_no_bindings_auto_appends_dependency_summaries() -> None:
    # The common case: templates/LLM plans express deps as EDGES, not bindings. A downstream node
    # must still see its upstreams' findings (else it runs blind on the bare goal).
    node = _node(node_id="write")  # no explicit input_bindings
    msg = render_node_message(node, "write it up", {"research": "the findings"}, dep_ids=["research"])
    assert "write it up" in msg
    assert "the findings" in msg


def test_auto_append_skips_missing_upstream() -> None:
    node = _node(node_id="write")
    assert render_node_message(node, "goal", {}, dep_ids=["research"]) == "goal"


# ── dependency_map (edges + node depends_on) ──────────────────────────────────────────────
def test_dependency_map_from_edges() -> None:
    dag = parse_dag(
        {
            "nodes": [
                {"node_id": "research", "node_type": "agent"},
                {"node_id": "write", "node_type": "agent"},
            ],
            "edges": [{"from_node": "research", "to_node": "write"}],
        }
    )
    deps = dependency_map(dag)
    assert deps == {"research": set(), "write": {"research"}}


# ── deps_satisfied ───────────────────────────────────────────────────────────────────────
def test_deps_satisfied_all_terminal() -> None:
    states = {"a": _state("completed"), "b": _state("skipped")}
    assert deps_satisfied({"a", "b"}, states) is True


def test_deps_satisfied_empty_is_true() -> None:
    assert deps_satisfied(set(), {}) is True


def test_deps_unsatisfied_on_failure_or_missing() -> None:
    assert deps_satisfied({"a"}, {"a": _state("failed")}) is False
    assert deps_satisfied({"a"}, {"a": _state("running")}) is False
    assert deps_satisfied({"a"}, {}) is False  # missing upstream


def test_blocked_dependency_is_not_satisfied() -> None:
    # A cascade-skipped (blocked) upstream must NOT satisfy a dependent — the skip propagates.
    states = {"a": _state("skipped")}
    assert deps_satisfied({"a"}, states) is True  # intentional skip satisfies
    assert deps_satisfied({"a"}, states, blocked={"a"}) is False  # blocked skip does not


# ── default synthesis (leaf summaries) ────────────────────────────────────────────────────
def test_default_synthesis_uses_leaves() -> None:
    dag = parse_dag(
        {
            "nodes": [
                {"node_id": "research", "node_type": "agent"},
                {"node_id": "write", "node_type": "agent"},
            ],
            "edges": [{"from_node": "research", "to_node": "write"}],
        }
    )
    out = _default_synthesis(dag, {"research": "R-summary", "write": "W-summary"})
    assert out == "W-summary"  # 'write' is the only leaf


# ── budget ceiling (B3) ──────────────────────────────────────────────────────────────────
def test_over_budget_cost_and_tokens() -> None:
    assert over_budget(0.5, None, 100, None) is False  # no caps
    assert over_budget(1.0, 1.0, 0, None) is True  # cost reached
    assert over_budget(0.5, 1.0, 0, None) is False  # under cost
    assert over_budget(0.0, None, 100, 100) is True  # tokens reached
    assert over_budget(0.0, None, 50, 100) is False  # under tokens


def test_over_budget_negative_is_uncapped() -> None:
    assert over_budget(5.0, -1.0, 999, -1) is False  # a negative ceiling means "no cap"


def test_remaining_cost() -> None:
    assert remaining_cost(None, 0.3) is None  # uncapped
    assert remaining_cost(1.0, 0.3) == 0.7
    assert remaining_cost(1.0, 1.5) == 0.0  # never negative


# ── cancel + timeout classification (final-review fixes) ─────────────────────────────────
def test_node_cancelled_detects_all_cancel_paths() -> None:
    cancelled_task = SubAgentResult(task_id="t", status="cancelled", summary=None)
    assert _node_cancelled(cancelled_task) is True
    assert _node_cancelled(SubAgentResult(task_id="t", status="completed", summary="ok")) is False
    # a HIL-gate cancel is surfaced as an ApiError carrying reason=CANCELLED
    cancel_err = ApiError(ErrorCode.SERVICE_UNAVAILABLE, "x", details={"reason": "CANCELLED"})
    assert _node_cancelled(cancel_err) is True
    assert _node_cancelled(ApiError(ErrorCode.FORBIDDEN, "x", details={"reason": "HIL_DENIED"})) is False
    assert _node_cancelled(ApiError(ErrorCode.INTERNAL_ERROR, "x")) is False  # no details


def test_deadline_parsing_and_past() -> None:
    assert _deadline_from(None) is None
    assert _deadline_from("not-a-date") is None
    dl = _deadline_from("2020-01-01T00:00:00.000Z")
    assert isinstance(dl, datetime)
    assert _past_deadline(dl) is True  # long past
    assert _past_deadline(None) is False  # no deadline set
    assert _past_deadline(datetime.now(UTC) + timedelta(hours=1)) is False  # future


def test_default_synthesis_parallel_join() -> None:
    dag = parse_dag(
        {
            "nodes": [
                {"node_id": "r1", "node_type": "agent"},
                {"node_id": "r2", "node_type": "agent"},
                {"node_id": "synthesis", "node_type": "agent"},
            ],
            "edges": [
                {"from_node": "r1", "to_node": "synthesis"},
                {"from_node": "r2", "to_node": "synthesis"},
            ],
        }
    )
    assert _default_synthesis(dag, {"r1": "A", "r2": "B", "synthesis": "S"}) == "S"
