"""WP12 — PROMPT_BUILD enhancement splice + prompt-context budget.

Drives the REAL :class:`PromptBuildStage`. No client needed (it reads ctx state +
settings). Coverage:
  * BYTE-IDENTICAL base prompt when there is NO enhancement context (no RAG/memory/skills)
    — the basic pipeline + the existing PROMPT_BUILD behaviour are unchanged, and NO
    ``context_truncated`` step is written;
  * enhancement context is spliced as a SYSTEM message AFTER the agent system prompt and
    BEFORE the user turn (user always ends the prompt);
  * the spliced context is bounded to ≤ ``prompt_context_budget_fraction`` (30%) of the
    agent's ``token_budget_per_task`` (estimated via chars/chars_per_token);
  * truncation order is RAG -> memory -> skills (RAG dropped first), dropping whole items;
  * a ``context_truncated`` step is emitted IFF something was dropped, carrying the per
    -section dropped counts.
"""

from __future__ import annotations

import time
from typing import Any

from agent_runtime.core.auth import Principal
from agent_runtime.core.config import get_settings
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages.prompt_build import PromptBuildStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_CONTEXT_TRUNCATED

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


def _agent(*, system_prompt: str = "You are helpful.", token_budget: int = 10000,
           skills: list[str] | None = None) -> AgentRuntime:
    return AgentRuntime(
        agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt=system_prompt,
        token_budget_per_task=token_budget, allowed_skills=skills or [],
    )


def _ctx(agent: AgentRuntime, *, prompt: str = "What is 2+2?") -> PipelineContext:
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="jwt",
        trace_id=TRACE_ID,
        request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": prompt}),
        agent=agent,
        prompt_text=prompt,
        steps=StepBuffer(),
        pool=None,
        started_monotonic=time.monotonic(),
    )


def _truncation_steps(ctx: PipelineContext) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_CONTEXT_TRUNCATED]


# ── byte-identical base prompt when there is no enhancement context ─────────────────
async def test_no_context_is_byte_identical_base_prompt() -> None:
    ctx = _ctx(_agent(system_prompt="You are helpful."))

    await PromptBuildStage().run(ctx)

    # Exactly the first-cycle two-message prompt: system then user, verbatim.
    assert ctx.messages == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What is 2+2?"},
    ]
    assert _truncation_steps(ctx) == []  # no context -> no truncation step ever


async def test_no_system_prompt_yields_only_user_message() -> None:
    ctx = _ctx(_agent(system_prompt=""))

    await PromptBuildStage().run(ctx)

    assert ctx.messages == [{"role": "user", "content": "What is 2+2?"}]
    assert _truncation_steps(ctx) == []


# ── enhancement context spliced between system prompt and user turn ─────────────────
async def test_context_spliced_after_system_before_user() -> None:
    ctx = _ctx(_agent(skills=["skill-a"]))
    ctx.rag_chunks = [{"kb_id": "kb-1", "text": "RAG fact", "score": 0.9}]
    ctx.memories = [{"content": "a memory", "score": 0.8}]

    await PromptBuildStage().run(ctx)

    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "system", "user"]  # agent-system, context-system, user
    # The user turn always ends the prompt.
    assert ctx.messages[-1] == {"role": "user", "content": "What is 2+2?"}
    context_block = ctx.messages[1]["content"]
    assert "RAG fact" in context_block
    assert "a memory" in context_block
    assert "skill-a" in context_block
    assert _truncation_steps(ctx) == []  # well within the 10000-token budget


# ── budget: context bounded to ≤30% of token_budget_per_task ────────────────────────
async def test_context_bounded_to_budget_fraction() -> None:
    settings = get_settings()
    # token_budget=100 -> char budget = 100 * 0.30 * 4 = 120 chars.
    char_budget = int(100 * settings.prompt_context_budget_fraction * settings.prompt_context_chars_per_token)
    assert char_budget == 120

    ctx = _ctx(_agent(token_budget=100))
    # Many large RAG chunks that vastly exceed 120 chars combined.
    ctx.rag_chunks = [{"kb_id": "k", "text": "X" * 100, "score": 0.9} for _ in range(10)]

    await PromptBuildStage().run(ctx)

    context_block = next(m["content"] for m in ctx.messages if m["role"] == "system")
    # The assembled context block fits the char budget (header excluded from the count;
    # the budget governs the rendered item LINES — assert it dropped down to within budget).
    rag_lines = [ln for ln in context_block.splitlines() if ln.startswith("- ")]
    # 120-char budget / (~104 chars per rendered line) -> at most 1 chunk survives.
    assert len(rag_lines) <= 1
    assert _truncation_steps(ctx)  # over-budget -> a truncation step was emitted


# ── truncation order RAG -> memory -> skills; whole items dropped ───────────────────
async def test_truncation_order_rag_then_memory_then_skills() -> None:
    # Budget allows only a couple of short lines; force drops across sections.
    # token_budget=40 -> char budget = 40*0.30*4 = 48 chars.
    ctx = _ctx(_agent(token_budget=40, skills=["s1", "s2"]))
    ctx.rag_chunks = [{"kb_id": "k", "text": "rag-one", "score": 0.9},
                      {"kb_id": "k", "text": "rag-two", "score": 0.8}]
    ctx.memories = [{"content": "mem-one", "score": 0.9}, {"content": "mem-two", "score": 0.8}]

    await PromptBuildStage().run(ctx)

    step = _truncation_steps(ctx)[0]
    dropped = step.output["dropped"]
    # RAG is dropped FIRST (highest-priority-to-drop). Given the tiny budget, RAG items go
    # before memory items, and memory before skills.
    assert dropped.get("rag", 0) >= 1
    # If memory was dropped at all, RAG must have been fully exhausted first.
    if dropped.get("memory", 0) > 0:
        assert dropped["rag"] == 2  # both RAG items gone before any memory item
    if dropped.get("skills", 0) > 0:
        assert dropped.get("memory", 0) == 2  # all memory gone before any skill


async def test_truncation_step_emitted_only_when_something_dropped() -> None:
    # Generous budget: nothing dropped -> NO truncation step despite having context.
    ctx = _ctx(_agent(token_budget=100000))
    ctx.rag_chunks = [{"kb_id": "k", "text": "small", "score": 0.9}]

    await PromptBuildStage().run(ctx)

    assert _truncation_steps(ctx) == []
    # And the context IS present (it just was not truncated).
    assert any(m["role"] == "system" and "small" in m["content"] for m in ctx.messages)


async def test_dropped_counts_recorded() -> None:
    # token_budget=8 -> char budget = 8*0.30*4 = 9 chars: almost everything dropped.
    ctx = _ctx(_agent(token_budget=8))
    ctx.rag_chunks = [{"kb_id": "k", "text": "aaaa", "score": 0.9},
                      {"kb_id": "k", "text": "bbbb", "score": 0.8}]

    await PromptBuildStage().run(ctx)

    step = _truncation_steps(ctx)[0]
    assert step.step_name == "context_truncated"
    assert step.status == "passed"
    # Only sections with a >0 drop count appear in the output.
    assert all(v > 0 for v in step.output["dropped"].values())
    assert step.output["dropped"].get("rag", 0) >= 1
