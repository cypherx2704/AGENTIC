"""Unit tests for the pure orchestration DAG utility (migration 0008).

Covers parsing, Kahn topological layering, cycle detection, referential integrity, and the
depth/fanout caps — the mandatory pre-execution validation that fails a malformed or cyclic
decomposition CLOSED before any sub-agent is spawned.
"""

from __future__ import annotations

import pytest

from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.orchestration.dag import (
    DEFAULT_MAX_DEPTH,
    parse_dag,
    topological_layers,
    validate_dag,
)


def _doc(nodes: list[dict], edges: list[dict] | None = None, constraints: dict | None = None) -> dict:
    doc: dict = {"nodes": nodes}
    if edges is not None:
        doc["edges"] = edges
    if constraints is not None:
        doc["constraints"] = constraints
    return doc


def _agents(*ids: str) -> list[dict]:
    return [{"node_id": i, "node_type": "agent", "ref": "research"} for i in ids]


# ── parsing ────────────────────────────────────────────────────────────────────────────
def test_parse_minimal_single_node() -> None:
    dag = parse_dag(_doc([{"node_id": "a", "node_type": "task"}]))
    assert set(dag.nodes) == {"a"}
    assert dag.nodes["a"].on_error == "fail"
    assert dag.nodes["a"].retry_max == 1
    assert dag.max_depth == DEFAULT_MAX_DEPTH


def test_parse_preserves_explicit_zero_retry() -> None:
    # An explicit retry.max of 0 (run once, no retries) must NOT be coerced to 1.
    dag = parse_dag(_doc([{"node_id": "a", "node_type": "agent", "retry": {"max": 0}}]))
    assert dag.nodes["a"].retry_max == 0


@pytest.mark.parametrize("doc", [None, [], "nope", 7])
def test_parse_non_object_fails_closed(doc: object) -> None:
    # A non-object subtask_dag (SQL NULL / JSON array / scalar) must fail CLOSED as INVALID_DAG,
    # never bubble an AttributeError into a 500.
    with pytest.raises(ApiError) as exc:
        parse_dag(doc)  # type: ignore[arg-type]  — deliberately exercising the non-dict guard
    assert exc.value.code == ErrorCode.INVALID_DAG


def test_constraints_cannot_raise_fanout_cap() -> None:
    # A DAG-supplied max_fanout above the default is clamped DOWN to the default ceiling.
    nodes = _agents(*[f"n{i}" for i in range(10)])  # 10 independent nodes -> one 10-wide layer
    with pytest.raises(ApiError) as exc:
        validate_dag(parse_dag(_doc(nodes, constraints={"max_fanout": 500})))
    assert exc.value.code == ErrorCode.INVALID_DAG
    assert exc.value.details == {"fanout": 10, "max_fanout": 8}  # clamped to DEFAULT_MAX_FANOUT


def test_constraints_cannot_raise_depth_cap() -> None:
    # A DAG-supplied max_depth above the default is clamped DOWN to the default ceiling.
    ids = [f"n{i}" for i in range(7)]  # a 7-deep chain > DEFAULT_MAX_DEPTH (5)
    edges = [{"from_node": ids[i], "to_node": ids[i + 1]} for i in range(len(ids) - 1)]
    with pytest.raises(ApiError) as exc:
        validate_dag(parse_dag(_doc(_agents(*ids), edges=edges, constraints={"max_depth": 50})))
    assert exc.value.code == ErrorCode.INVALID_DAG
    assert exc.value.details == {"depth": 7, "max_depth": 5}  # clamped to DEFAULT_MAX_DEPTH


def test_parse_reads_node_fields() -> None:
    dag = parse_dag(
        _doc(
            [
                {
                    "node_id": "n",
                    "node_type": "agent",
                    "ref": "generate",
                    "assigned_agent_id": "9f8b5d2e-1c3a-4f6b-8a7e-2d4c6e8f0a1b",
                    "preset": "writer",
                    "input_bindings": {"context": "{{a.output}}"},
                    "timeout_seconds": 120,
                    "retry": {"max": 3},
                    "on_error": "skip",
                    "depends_on": ["a"],
                }
            ]
        )
    )
    node = dag.nodes["n"]
    assert node.preset == "writer"
    assert node.retry_max == 3
    assert node.on_error == "skip"
    assert node.timeout_seconds == 120
    assert node.depends_on == ("a",)


@pytest.mark.parametrize(
    "doc",
    [
        {"nodes": []},
        {"nodes": "notalist"},
        {},
        {"nodes": [{"node_type": "agent"}]},  # missing node_id
        {"nodes": [{"node_id": "", "node_type": "agent"}]},  # blank node_id
        {"nodes": [{"node_id": "a", "node_type": "supervisor"}]},  # bad node_type
        {"nodes": [{"node_id": "a", "node_type": "agent"}, {"node_id": "a", "node_type": "task"}]},  # dup
        {"nodes": [{"node_id": "a", "node_type": "agent", "on_error": "explode"}]},  # bad on_error
    ],
)
def test_parse_rejects_malformed(doc: dict) -> None:
    with pytest.raises(ApiError) as exc:
        parse_dag(doc)
    assert exc.value.code == ErrorCode.INVALID_DAG


def test_parse_rejects_bad_edge() -> None:
    with pytest.raises(ApiError) as exc:
        parse_dag(_doc(_agents("a", "b"), edges=[{"from_node": "a"}]))
    assert exc.value.code == ErrorCode.INVALID_DAG


# ── topological layering ─────────────────────────────────────────────────────────────────
def test_layers_all_independent_is_single_layer() -> None:
    dag = parse_dag(_doc(_agents("a", "b", "c")))
    assert topological_layers(dag) == [["a", "b", "c"]]


def test_layers_sequential_chain() -> None:
    edges = [{"from_node": "a", "to_node": "b"}, {"from_node": "b", "to_node": "c"}]
    dag = parse_dag(_doc(_agents("a", "b", "c"), edges=edges))
    assert topological_layers(dag) == [["a"], ["b"], ["c"]]


def test_layers_diamond() -> None:
    dag = parse_dag(
        _doc(
            _agents("a", "b", "c", "d"),
            edges=[
                {"from_node": "a", "to_node": "b"},
                {"from_node": "a", "to_node": "c"},
                {"from_node": "b", "to_node": "d"},
                {"from_node": "c", "to_node": "d"},
            ],
        )
    )
    assert topological_layers(dag) == [["a"], ["b", "c"], ["d"]]


def test_duplicate_edges_do_not_break_indegree() -> None:
    # A repeated edge must not double-count in-degree (else the dependent never resolves).
    dag = parse_dag(
        _doc(
            _agents("a", "b"),
            edges=[{"from_node": "a", "to_node": "b"}, {"from_node": "a", "to_node": "b"}],
        )
    )
    assert topological_layers(dag) == [["a"], ["b"]]


def test_depends_on_and_edges_combine() -> None:
    dag = parse_dag(
        _doc(
            [
                {"node_id": "a", "node_type": "agent"},
                {"node_id": "b", "node_type": "agent", "depends_on": ["a"]},
            ]
        )
    )
    assert topological_layers(dag) == [["a"], ["b"]]


# ── cycle detection ──────────────────────────────────────────────────────────────────────
def test_self_loop_is_a_cycle() -> None:
    dag = parse_dag(_doc(_agents("a"), edges=[{"from_node": "a", "to_node": "a"}]))
    with pytest.raises(ApiError) as exc:
        topological_layers(dag)
    assert exc.value.code == ErrorCode.INVALID_DAG


def test_two_node_cycle() -> None:
    edges = [{"from_node": "a", "to_node": "b"}, {"from_node": "b", "to_node": "a"}]
    dag = parse_dag(_doc(_agents("a", "b"), edges=edges))
    with pytest.raises(ApiError):
        validate_dag(dag)


# ── validation: referential integrity + caps ─────────────────────────────────────────────
def test_validate_rejects_edge_to_unknown_node() -> None:
    dag = parse_dag(_doc(_agents("a"), edges=[{"from_node": "a", "to_node": "ghost"}]))
    with pytest.raises(ApiError) as exc:
        validate_dag(dag)
    assert exc.value.code == ErrorCode.INVALID_DAG


def test_validate_enforces_max_depth() -> None:
    dag = parse_dag(
        _doc(
            _agents("a", "b", "c"),
            edges=[{"from_node": "a", "to_node": "b"}, {"from_node": "b", "to_node": "c"}],
            constraints={"max_depth": 2},
        )
    )
    with pytest.raises(ApiError) as exc:
        validate_dag(dag)
    assert exc.value.code == ErrorCode.INVALID_DAG
    assert exc.value.details == {"depth": 3, "max_depth": 2}


def test_validate_enforces_max_fanout() -> None:
    dag = parse_dag(_doc(_agents("a", "b", "c"), constraints={"max_fanout": 2}))
    with pytest.raises(ApiError) as exc:
        validate_dag(dag)
    assert exc.value.code == ErrorCode.INVALID_DAG
    assert exc.value.details == {"fanout": 3, "max_fanout": 2}


def test_validate_ok_returns_layers() -> None:
    dag = parse_dag(_doc(_agents("a", "b"), edges=[{"from_node": "a", "to_node": "b"}]))
    assert validate_dag(dag) == [["a"], ["b"]]
