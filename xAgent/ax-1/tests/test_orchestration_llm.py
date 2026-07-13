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
