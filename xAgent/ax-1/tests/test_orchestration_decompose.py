"""Unit tests for the goal decomposer — the PLANNER decides; the decomposer only validates.

The load-bearing invariant across this file: **no code path here ever picks an agent.** The planner
picks, or the run fails. There is no keyword router, no preset template, and no substitute agent when
the planner gets it wrong — only a repair attempt and, failing that, ``ORCHESTRATION_FAILED``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.orchestration.dag import parse_dag, validate_dag
from agent_runtime.orchestration.decompose import (
    Planner,
    build_solo_dag,
    decompose,
    plan_to_dag,
    validate_targets,
)

WF = "b1e7c2a4-3d5f-4a6b-8c9d-0e1f2a3b4c5d"
TEN = "550e8400-e29b-41d4-a716-446655440000"

#: The roster the planner may route to in these tests (sub-agents + the no-delegation target).
TARGETS = ["orchestrator", "researcher", "writer"]


def _valid_layers(doc: dict[str, Any]) -> list[list[str]]:
    """Every produced DAG must parse + validate; returns its layers for assertions."""
    return validate_dag(parse_dag(doc))


def _planner(*plans: Any) -> tuple[Planner, list[str | None]]:
    """A fake planner returning ``plans`` in order; records the feedback it was called with.

    A plan may be an Exception, which is raised instead of returned (to simulate a planner blowing up
    or returning unparseable JSON).
    """
    seen: list[str | None] = []
    queue = list(plans)

    async def planner(_goal: str, feedback: str | None = None) -> dict[str, Any]:
        seen.append(feedback)
        nxt = queue.pop(0) if queue else {"steps": []}
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[no-any-return]

    return planner, seen


def _step(node_id: str, preset: str, *deps: str) -> dict[str, Any]:
    return {"id": node_id, "step": f"do {node_id}", "preset": preset, "depends_on": list(deps)}


# ── the solo (no-delegation) graph ───────────────────────────────────────────────────────
def test_solo_dag_is_valid_and_carries_no_preset() -> None:
    """The solo node must have NO preset: that is what makes the driver bind it to the ORCHESTRATOR
    (via default_agent_id) rather than to some sub-agent."""
    doc = build_solo_dag(goal="do a thing", workflow_id=WF, tenant_id=TEN)
    assert _valid_layers(doc) == [["main"]]
    assert doc["workflow_id"] == WF and doc["tenant_id"] == TEN
    assert "preset" not in doc["nodes"][0]


# ── plan_to_dag: a mechanical translation, nothing inferred ──────────────────────────────
def test_plan_to_dag_builds_edges_from_depends_on() -> None:
    plan = {"steps": [_step("a", "researcher"), _step("b", "writer", "a")]}
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


# ── validate_targets: reports, never re-routes ───────────────────────────────────────────
def test_validate_targets_accepts_real_targets() -> None:
    doc = plan_to_dag({"steps": [_step("a", "researcher")]}, goal="g", workflow_id=WF, tenant_id=TEN)
    validate_targets(doc, TARGETS)  # does not raise


def test_validate_targets_rejects_an_unknown_agent_and_names_the_alternatives() -> None:
    """The planner named an agent that does not exist -> UNKNOWN_AGENT, never a substitution."""
    doc = plan_to_dag({"steps": [_step("a", "resercher")]}, goal="g", workflow_id=WF, tenant_id=TEN)
    with pytest.raises(ApiError) as exc:
        validate_targets(doc, TARGETS)
    assert exc.value.code == ErrorCode.UNKNOWN_AGENT
    # The message must be actionable enough for the planner to repair itself.
    assert "resercher" in exc.value.message
    assert "researcher" in exc.value.message and "orchestrator" in exc.value.message


def test_validate_targets_rejects_a_step_with_no_target() -> None:
    """A missing preset is a malformed plan, NOT an invitation for the backend to choose. Distinct
    from UNKNOWN_AGENT: nothing was named at all, so no decision was overridden."""
    doc = plan_to_dag({"steps": [{"id": "a", "step": "x"}]}, goal="g", workflow_id=WF, tenant_id=TEN)
    with pytest.raises(ApiError) as exc:
        validate_targets(doc, TARGETS)
    assert exc.value.code == ErrorCode.INVALID_DAG


def test_validate_targets_skips_when_roster_unknown() -> None:
    doc = plan_to_dag({"steps": [_step("a", "anything")]}, goal="g", workflow_id=WF, tenant_id=TEN)
    validate_targets(doc, None)  # roster unknown -> no check


# ── decompose: mode / no-planner ─────────────────────────────────────────────────────────
async def test_solo_mode_never_calls_the_planner() -> None:
    planner, seen = _planner({"steps": [_step("a", "researcher")]})
    d = await decompose("anything", workflow_id=WF, tenant_id=TEN, mode="solo", planner=planner)
    assert d.decomposition == "template" and d.template == "solo"
    assert _valid_layers(d.dag_doc) == [["main"]]
    assert seen == []  # the user opted out of delegation; no planning call was spent


async def test_blank_goal_falls_back_to_solo() -> None:
    d = await decompose("   ", workflow_id=TEN, tenant_id=TEN)
    assert d.template == "solo"


async def test_no_planner_delegates_to_nobody() -> None:
    """With no model there is nobody to make a routing decision — and the backend may not make one
    on its behalf. It runs solo rather than guessing at an agent."""
    d = await decompose("Research X and write a brief.", workflow_id=WF, tenant_id=TEN, planner=None)
    assert d.decomposition == "template" and d.template == "solo"


# ── decompose: the planner owns the split AND the routing ────────────────────────────────
async def test_planner_plan_is_used_verbatim() -> None:
    planner, _ = _planner({"steps": [_step("a", "researcher"), _step("b", "writer", "a")]})
    d = await decompose("do something", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert d.decomposition == "llm"
    assert _valid_layers(d.dag_doc) == [["a"], ["b"]]
    assert [n["preset"] for n in d.dag_doc["nodes"]] == ["researcher", "writer"]


async def test_planner_may_return_a_single_step_no_fanout() -> None:
    """A 1-step plan is VALID. An earlier prompt demanded 2-5 steps, so the orchestrator was
    structurally incapable of saying 'one agent is enough' and invented work to fill the quota."""
    planner, _ = _planner({"steps": [_step("s1", "researcher")]})
    d = await decompose(
        "Research the Eiffel Tower and do NOT write a brief.",
        workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS,
    )
    assert d.decomposition == "llm"
    assert len(d.dag_doc["nodes"]) == 1  # no gratuitous fan-out
    assert d.dag_doc["nodes"][0]["preset"] == "researcher"


async def test_planner_may_target_the_orchestrator_itself() -> None:
    """'No delegation needed' is a first-class outcome, reachable even with a full roster."""
    planner, _ = _planner({"steps": [_step("s1", "orchestrator")]})
    d = await decompose("What is 2 + 2?", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert len(d.dag_doc["nodes"]) == 1
    assert d.dag_doc["nodes"][0]["preset"] == "orchestrator"


async def test_planner_owns_concurrency() -> None:
    """Parallelism is whatever the planner's depends_on says it is — nothing groups steps by type."""
    planner, _ = _planner(
        {"steps": [_step("a", "researcher"), _step("b", "researcher"), _step("j", "writer", "a", "b")]}
    )
    d = await decompose("compare X and Y", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    layers = _valid_layers(d.dag_doc)
    assert set(layers[0]) == {"a", "b"}  # concurrent because the PLAN says they are independent
    assert layers[1] == ["j"]


async def test_a_custom_roster_is_routed_to_by_its_real_names() -> None:
    planner, _ = _planner({"steps": [_step("s1", "wiki-bot"), _step("s2", "analyst", "s1")]})
    d = await decompose(
        "Research X and write a brief.", workflow_id=WF, tenant_id=TEN, planner=planner,
        targets=["orchestrator", "wiki-bot", "analyst"],
    )
    assert {n["preset"] for n in d.dag_doc["nodes"]} == {"wiki-bot", "analyst"}


# ── decompose: the repair loop ───────────────────────────────────────────────────────────
async def test_invalid_target_is_repaired_by_the_planner_not_the_backend() -> None:
    """The planner names an agent that does not exist. The backend must NOT substitute one — it
    hands the plan back with the reason and the planner fixes it."""
    planner, seen = _planner(
        {"steps": [_step("s1", "resercher")]},   # typo -> rejected
        {"steps": [_step("s1", "researcher")]},  # repaired
    )
    d = await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert d.decomposition == "llm"
    assert d.dag_doc["nodes"][0]["preset"] == "researcher"
    assert seen[0] is None  # first attempt: no feedback
    assert seen[1] is not None and "resercher" in seen[1]  # repair turn names what was wrong
    assert "researcher" in seen[1]  # ...and what was allowed


async def test_cyclic_plan_is_repaired() -> None:
    planner, seen = _planner(
        {"steps": [_step("a", "researcher", "b"), _step("b", "writer", "a")]},  # cycle
        {"steps": [_step("a", "researcher"), _step("b", "writer", "a")]},
    )
    d = await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert d.decomposition == "llm"
    assert seen[1] is not None and "cycle" in seen[1].lower()


async def test_second_failure_hard_fails_the_run() -> None:
    """Two bad plans in a row = ORCHESTRATION_FAILED. It does NOT quietly fall back to an agent."""
    planner, _ = _planner(
        {"steps": [_step("s1", "ghost")]},
        {"steps": [_step("s1", "still-a-ghost")]},
    )
    with pytest.raises(ApiError) as exc:
        await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert exc.value.code == ErrorCode.ORCHESTRATION_FAILED
    assert "Agent orchestration failed" in exc.value.message


async def test_planner_exception_is_retried_then_hard_fails() -> None:
    planner, _ = _planner(RuntimeError("llm exploded"), RuntimeError("llm exploded again"))
    with pytest.raises(ApiError) as exc:
        await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert exc.value.code == ErrorCode.ORCHESTRATION_FAILED


async def test_planner_exception_then_a_good_plan_succeeds() -> None:
    planner, _ = _planner(RuntimeError("transient"), {"steps": [_step("s1", "researcher")]})
    d = await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert d.decomposition == "llm"


async def test_only_one_repair_is_attempted() -> None:
    planner, seen = _planner(
        {"steps": [_step("s1", "ghost")]},
        {"steps": [_step("s1", "ghost")]},
        {"steps": [_step("s1", "researcher")]},  # a third plan would be fine — but is never asked for
    )
    with pytest.raises(ApiError):
        await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert len(seen) == 2  # exactly one plan + one repair


# ── decompose: the retry-approval gate ───────────────────────────────────────────────────
async def test_retry_approval_is_asked_with_the_reason() -> None:
    planner, _ = _planner({"steps": [_step("s1", "ghost")]}, {"steps": [_step("s1", "researcher")]})
    asked: list[str] = []

    async def approve(reason: str) -> bool:
        asked.append(reason)
        return True

    d = await decompose(
        "do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS, approve_retry=approve
    )
    assert d.decomposition == "llm"
    assert len(asked) == 1 and "ghost" in asked[0]  # the human is TOLD what was wrong


async def test_declined_retry_hard_fails_without_replanning() -> None:
    """An explicit human 'no' stops the run. No second planning call, no substitute agent."""
    planner, seen = _planner({"steps": [_step("s1", "ghost")]}, {"steps": [_step("s1", "researcher")]})

    async def deny(_reason: str) -> bool:
        return False

    with pytest.raises(ApiError) as exc:
        await decompose(
            "do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS,
            approve_retry=deny,
        )
    assert exc.value.code == ErrorCode.ORCHESTRATION_FAILED
    assert "declined" in exc.value.message
    assert len(seen) == 1  # the planner was never asked again


async def test_a_broken_approval_channel_is_not_a_refusal() -> None:
    """An approver that ERRORS means 'nobody could be asked', not 'a human said no' — so the retry
    proceeds rather than a run dying because the approval service was down."""
    planner, _ = _planner({"steps": [_step("s1", "ghost")]}, {"steps": [_step("s1", "researcher")]})

    async def broken(_reason: str) -> bool:
        raise RuntimeError("hil is down")

    d = await decompose(
        "do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS, approve_retry=broken
    )
    assert d.decomposition == "llm"


async def test_no_approver_retries_without_asking() -> None:
    planner, _ = _planner({"steps": [_step("s1", "ghost")]}, {"steps": [_step("s1", "researcher")]})
    d = await decompose("do it", workflow_id=WF, tenant_id=TEN, planner=planner, targets=TARGETS)
    assert d.decomposition == "llm"


# ── the deleted machinery must stay deleted ──────────────────────────────────────────────
def test_no_keyword_router_or_preset_templates_exist() -> None:
    """Regression guard for the architectural rule: the backend must contain NO forced routing.

    These names were `if goal contains "compare": assign three researchers` — a routing rule that
    ignored the LLM entirely, and one that could not even read a negation. If any of them comes back,
    this test fails."""
    import agent_runtime.orchestration.decompose as d

    for banned in (
        "match_template",           # keyword -> template router
        "TEMPLATES",                # the research-write / parallel-research registry
        "TEMPLATE_PRESETS",
        "template_is_satisfiable",
        "build_template_dag",
        "PARALLEL_RESEARCH_BRANCHES",  # a hardcoded 3-way fan-out
    ):
        assert not hasattr(d, banned), f"forced-routing symbol is back: {banned}"
