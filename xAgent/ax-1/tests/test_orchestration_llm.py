"""Unit tests for the orchestrator LLM glue (phase B5) — plan parsing, planner, synthesis."""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.orchestration.llm import (
    LlmResult,
    make_llm_planner,
    parse_plan,
    synthesize,
)


def _complete(content: str, *, tokens: int = 0, cost: float = 0.0):
    async def complete(_messages: list[dict[str, Any]]) -> LlmResult:
        return LlmResult(content=content, tokens_used=tokens, cost_usd=cost)

    return complete


def _raises():
    async def complete(_messages: list[dict[str, Any]]) -> LlmResult:
        raise RuntimeError("llm down")

    return complete


# ── parse_plan ───────────────────────────────────────────────────────────────────────────
def test_parse_plain_json() -> None:
    assert parse_plan('{"steps": [{"id": "a", "step": "do a"}]}')["steps"][0]["id"] == "a"


def test_parse_fenced_json() -> None:
    text = '```json\n{"steps": [{"id": "a", "step": "x"}]}\n```'
    assert parse_plan(text)["steps"][0]["id"] == "a"


def test_parse_json_with_surrounding_prose() -> None:
    text = 'Here is the plan:\n{"steps": [{"id": "a", "step": "x"}]}\nHope that helps!'
    assert parse_plan(text)["steps"][0]["step"] == "x"


@pytest.mark.parametrize("bad", ["not json at all", "[1,2,3]", "{oops"])
def test_parse_rejects_bad(bad: str) -> None:
    with pytest.raises((ValueError, Exception)):
        parse_plan(bad)


# ── make_llm_planner ─────────────────────────────────────────────────────────────────────
async def test_planner_returns_parsed_plan() -> None:
    planner = make_llm_planner(_complete('{"steps": [{"id": "r", "step": "research"}]}'))
    plan = await planner("do a thing")
    assert plan["steps"][0]["id"] == "r"


# ── synthesize ───────────────────────────────────────────────────────────────────────────
async def test_synthesize_empty_when_no_summaries() -> None:
    r = await synthesize("goal", {}, complete=None)
    assert r.content == ""


async def test_synthesize_joins_without_llm() -> None:
    r = await synthesize("goal", {"a": "finding A", "b": "finding B"}, complete=None)
    assert "finding A" in r.content and "finding B" in r.content
    assert r.tokens_used == 0


async def test_synthesize_uses_llm_and_reports_usage() -> None:
    r = await synthesize(
        "goal", {"a": "x"}, complete=_complete("the synthesized answer", tokens=120, cost=0.02)
    )
    assert r.content == "the synthesized answer"
    assert r.tokens_used == 120 and r.cost_usd == 0.02


async def test_synthesize_falls_back_on_llm_error() -> None:
    r = await synthesize("goal", {"a": "finding A"}, complete=_raises())
    assert r.content == "finding A"  # joined fallback, never raises


async def test_synthesize_falls_back_on_empty_llm_output() -> None:
    r = await synthesize("goal", {"a": "finding A"}, complete=_complete("   "))
    assert r.content == "finding A"


# ── capability-aware routing (the "GitHub stats -> wiki-researcher" bug) ─────────────────
def test_plan_prompt_lists_each_agents_tools() -> None:
    """The planner must ROUTE BY CAPABILITY. Given only names it guesses from the string and hands
    a GitHub lookup to an agent holding just a Wikipedia tool — which then answers with no tool
    call at all. The prompt therefore has to carry each agent's purpose AND its actual tools.
    """
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    prompt = _plan_system([
        AgentCapability(
            name="wiki-researcher",
            purpose="You research topics using the Wikipedia tool.",
            tools=("tool-wikipedia-summary-cabd9d62",),
        ),
        AgentCapability(
            name="repo-analyst",
            purpose="You look up GitHub repository statistics.",
            tools=("tool-github-repo-info-cabd9d62",),
        ),
        AgentCapability(name="brief-writer", purpose="You write the final answer.", tools=()),
    ])

    # Each agent's TOOLS are visible, so a GitHub step can be routed to the agent that can do it.
    assert "tool-github-repo-info-cabd9d62" in prompt
    assert "tool-wikipedia-summary-cabd9d62" in prompt
    # A toolless agent is explicitly marked as such, not left ambiguous.
    assert "NONE (cannot call any tool)" in prompt
    # Purposes are carried.
    assert "GitHub repository statistics" in prompt
    # The no-delegation target is always offered.
    assert "orchestrator" in prompt


def test_plan_prompt_forbids_routing_to_an_incapable_agent() -> None:
    """The routing rule that stops a capability-less agent inventing an answer."""
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    prompt = _plan_system([AgentCapability(name="a", purpose="p", tools=())])
    assert "Never assign a step to an agent that lacks the required tool" in prompt
    assert "would invent an answer" in prompt


def test_plan_prompt_allows_no_delegation_and_forbids_inventing_steps() -> None:
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    prompt = _plan_system([AgentCapability(name="a", tools=())])
    assert "The DEFAULT is NO delegation" in prompt
    assert "NEVER add a step the goal did not ask for" in prompt
    assert "NEGATIONS" in prompt


def test_plan_prompt_routes_on_the_description_as_well_as_the_tools() -> None:
    """Both signals, because neither is sufficient alone: tools cannot tell two TOOLLESS agents
    apart (a writer and a reviewer both show `tools: NONE`), and a description cannot stop a step
    being sent to an agent that has no tool to do it."""
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    prompt = _plan_system([
        AgentCapability(name="writer", purpose="Turn findings into clean prose.", tools=()),
        AgentCapability(name="reviewer", purpose="Critique a draft for errors.", tools=()),
    ])
    assert "Turn findings into clean prose." in prompt
    assert "Critique a draft for errors." in prompt
    assert "read BOTH the description and the tools" in prompt


def test_plan_prompt_marks_an_undescribed_agent_as_unspecified() -> None:
    """An agent with no description is named, not explained. Say so, rather than papering over it —
    routing there would be a guess, and the planner should treat it as one."""
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    prompt = _plan_system([AgentCapability(name="mystery", purpose="", tools=())])
    assert "UNSPECIFIED — no description was configured" in prompt


def test_plan_prompt_never_hardcodes_agent_names() -> None:
    """REGRESSION GUARD. The old roster-free prompt named a fixed researcher|writer|reviewer trio
    and demanded '2-5 steps' — the backend telling the model to delegate, to agents that might not
    even exist. The prompt describes the choices; it must never prescribe one."""
    from agent_runtime.orchestration.llm import AgentCapability, _plan_system

    for roster in ([], [AgentCapability(name="wiki-bot", purpose="p", tools=())]):
        prompt = _plan_system(roster)
        assert "researcher|writer|reviewer" not in prompt
        assert "2-5 steps" not in prompt
        assert "prefer a researcher" not in prompt


def test_plan_prompt_with_an_empty_roster_still_offers_the_orchestrator() -> None:
    """An orchestrator with NO sub-agents is legitimate: it answers the goal itself. The catalogue
    must therefore always contain the no-delegation target, and nothing else to invent."""
    from agent_runtime.orchestration.llm import _plan_system

    prompt = _plan_system([])
    assert "orchestrator" in prompt
    assert "no sub-agent is needed" in prompt


def test_plan_prompt_states_the_graph_limits_it_is_validated_against() -> None:
    """The caps live in dag.py and are enforced there; the prompt must quote the SAME numbers, or a
    plan gets rejected for a rule the model was never told."""
    from agent_runtime.orchestration.dag import DEFAULT_MAX_DEPTH, DEFAULT_MAX_FANOUT
    from agent_runtime.orchestration.llm import _plan_system

    prompt = _plan_system([])
    assert f"At most {DEFAULT_MAX_FANOUT} steps may run in parallel" in prompt
    assert f"at most {DEFAULT_MAX_DEPTH} steps deep" in prompt
    assert "ACYCLIC" in prompt


# ── the repair channel ───────────────────────────────────────────────────────────────────
async def test_planner_appends_repair_feedback_as_a_new_turn() -> None:
    """A rejected plan goes BACK to the model with the reason — that is how routing stays the
    model's decision even when the model got it wrong."""
    seen: list[list[dict[str, Any]]] = []

    async def complete(messages: list[dict[str, Any]]) -> LlmResult:
        seen.append(messages)
        return LlmResult(content='{"steps": [{"id": "r", "step": "x", "preset": "orchestrator"}]}')

    planner = make_llm_planner(complete)
    await planner("do a thing", "Your previous plan was REJECTED: 'ghost' is not an agent.")

    contents = [m["content"] for m in seen[0]]
    assert any("Goal:\ndo a thing" in c for c in contents)
    assert any("REJECTED" in c and "ghost" in c for c in contents)


async def test_planner_sends_no_feedback_turn_on_the_first_attempt() -> None:
    seen: list[list[dict[str, Any]]] = []

    async def complete(messages: list[dict[str, Any]]) -> LlmResult:
        seen.append(messages)
        return LlmResult(content='{"steps": []}')

    planner = make_llm_planner(complete)
    await planner("do a thing")
    assert len(seen[0]) == 2  # system + goal, nothing else
