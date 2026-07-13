"""Unit tests for the goal decomposer (phase B1) — deterministic templates + LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.orchestration.dag import parse_dag, validate_dag
from agent_runtime.orchestration.decompose import (
    TEMPLATES,
    Planner,
    build_template_dag,
    decompose,
    match_template,
    plan_to_dag,
)

WF = "b1e7c2a4-3d5f-4a6b-8c9d-0e1f2a3b4c5d"
TEN = "550e8400-e29b-41d4-a716-446655440000"


def _valid_layers(doc: dict[str, Any]) -> list[list[str]]:
    """Every produced DAG must parse + validate; returns its layers for assertions."""
    return validate_dag(parse_dag(doc))


# ── template registry ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", list(TEMPLATES))
def test_every_template_is_a_valid_dag(name: str) -> None:
    doc = build_template_dag(name, goal="do a thing", workflow_id=WF, tenant_id=TEN)
    layers = _valid_layers(doc)
    assert layers  # non-empty schedule
    assert doc["workflow_id"] == WF and doc["tenant_id"] == TEN


def test_research_write_review_is_sequential() -> None:
    doc = build_template_dag("research-write-review", goal="g", workflow_id=WF, tenant_id=TEN)
    assert _valid_layers(doc) == [["research"], ["write"], ["review"]]


def test_parallel_research_fans_out_then_joins() -> None:
    doc = build_template_dag("parallel-research", goal="g", workflow_id=WF, tenant_id=TEN)
    layers = _valid_layers(doc)
    assert layers[-1] == ["synthesis"]
    assert set(layers[0]) == {"research-1", "research-2", "research-3"}


# ── deterministic router ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("research RAG advances and write a brief", "research-write"),
        ("draft a summary and review it", "research-write-review"),
        ("compare RAG and fine-tuning and summarize the tradeoffs", "parallel-research"),
        ("survey the landscape and write a report", "parallel-research"),
        ("what is the capital of France", None),
        ("just chat with me", None),
        ("", None),
    ],
)
def test_match_template(goal: str, expected: str | None) -> None:
    assert match_template(goal) == expected


def test_match_never_returns_solo() -> None:
    # solo is the explicit fallback, never a heuristic match.
    for goal in ["do work", "research and write", "compare and review and draft"]:
        assert match_template(goal) != "solo"


# ── plan_to_dag ──────────────────────────────────────────────────────────────────────────
def test_plan_to_dag_builds_edges_from_depends_on() -> None:
    plan = {"steps": [{"id": "a", "step": "gather"}, {"id": "b", "step": "write", "depends_on": ["a"]}]}
    doc = plan_to_dag(plan, goal="g", workflow_id=WF, tenant_id=TEN)
    assert _valid_layers(doc) == [["a"], ["b"]]


def test_plan_to_dag_synthesizes_ids_when_absent() -> None:
    plan = {"steps": [{"step": "one"}, {"step": "two", "depends_on": ["step-1"]}]}
    doc = plan_to_dag(plan, goal="g", workflow_id=WF, tenant_id=TEN)
    assert _valid_layers(doc) == [["step-1"], ["step-2"]]


@pytest.mark.parametrize("plan", [{}, {"steps": []}, {"steps": "nope"}, {"steps": [1, 2]}])
def test_plan_to_dag_rejects_unusable(plan: dict[str, Any]) -> None:
    with pytest.raises(ApiError) as exc:
        plan_to_dag(plan, goal="g", workflow_id=WF, tenant_id=TEN)
    assert exc.value.code == ErrorCode.INVALID_DAG


# ── decompose (the entrypoint) ───────────────────────────────────────────────────────────
async def test_solo_mode_uses_solo_template() -> None:
    d = await decompose("anything", workflow_id=WF, tenant_id=TEN, mode="solo")
    assert d.decomposition == "template" and d.template == "solo"
    assert _valid_layers(d.dag_doc) == [["main"]]


async def test_blank_goal_falls_back_to_solo() -> None:
    d = await decompose("   ", workflow_id=WF, tenant_id=TEN)
    assert d.template == "solo"


async def test_explicit_template() -> None:
    d = await decompose("g", workflow_id=WF, tenant_id=TEN, template="research-write")
    assert d.template == "research-write" and d.decomposition == "template"


async def test_unknown_explicit_template_is_validation_error() -> None:
    with pytest.raises(ApiError) as exc:
        await decompose("g", workflow_id=WF, tenant_id=TEN, template="nope")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


async def test_matched_goal_skips_llm() -> None:
    called = False

    async def planner(_goal: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"steps": [{"step": "x"}]}

    d = await decompose("research X and write a brief", workflow_id=WF, tenant_id=TEN, planner=planner)
    assert d.decomposition == "template" and d.template == "research-write"
    assert called is False  # deterministic match must NOT spend an LLM call


async def test_no_match_no_planner_is_solo() -> None:
    d = await decompose("ponder the universe", workflow_id=WF, tenant_id=TEN)
    assert d.template == "solo"


async def test_no_match_uses_planner() -> None:
    async def planner(_goal: str) -> dict[str, Any]:
        return {"steps": [{"id": "a", "step": "one"}, {"id": "b", "step": "two", "depends_on": ["a"]}]}

    d = await decompose("do something novel", workflow_id=WF, tenant_id=TEN, planner=planner)
    assert d.decomposition == "llm"
    assert _valid_layers(d.dag_doc) == [["a"], ["b"]]


async def test_planner_exception_degrades_to_solo() -> None:
    async def planner(_goal: str) -> dict[str, Any]:
        raise RuntimeError("llm exploded")

    d = await decompose("do something novel", workflow_id=WF, tenant_id=TEN, planner=planner)
    assert d.decomposition == "template" and d.template == "solo"


async def test_cyclic_plan_degrades_to_solo() -> None:
    # A cyclic LLM plan must not fail the run — it degrades to solo.
    async def planner(_goal: str) -> dict[str, Any]:
        return {
            "steps": [
                {"id": "a", "step": "a", "depends_on": ["b"]},
                {"id": "b", "step": "b", "depends_on": ["a"]},
            ]
        }

    d = await decompose("do something novel", workflow_id=WF, tenant_id=TEN, planner=planner)
    assert d.template == "solo"


# planner type is exported for the driver (B2) to satisfy — a light structural check.
def test_planner_type_is_exported() -> None:
    assert Planner is not None
