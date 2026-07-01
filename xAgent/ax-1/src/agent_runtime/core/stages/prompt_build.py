"""PROMPT_BUILD stage — assemble the LLM message list + splice enhancement context (WP12).

BASE behaviour (unchanged from the first cycle): build ``ctx.messages`` as a two-message
chat — the agent's configured system prompt, then the (possibly redacted) user message.
When NO enhancement context is present (no RAG chunks, memories, skills) the output is
BYTE-IDENTICAL to the first cycle and NO audit step is written, so the basic pipeline +
the existing tests are unchanged.

ENHANCEMENT SPLICE (WP12) — only when an upstream enhancement stage stashed context on the
PipelineContext:
  * RAG chunks (``ctx.rag_chunks``, from RAG_QUERY),
  * memories (``ctx.memories``, from MEMORY_RETRIEVE),
  * skills (``ctx.agent.allowed_skills`` — SKILL_LOAD is a later phase; we splice the
    configured skill names as the lowest-priority context so the budget order is complete).
The spliced context is injected as an ADDITIONAL system message placed AFTER the agent's
system prompt and BEFORE the user message, so the user's turn always ends the prompt.

PROMPT-CONTEXT BUDGET: the spliced context may consume at most
``settings.prompt_context_budget_fraction`` (default 30%) of the agent's
``token_budget_per_task``. Token cost is estimated heuristically (chars /
``prompt_context_chars_per_token``) to avoid a tokenizer on the hot path. When the assembled
context exceeds the budget it is TRUNCATED in the order RAG -> memory -> skills (RAG dropped
first, then memory, then skills — least-to-most agent-authored), dropping whole items from
the END of each section until it fits. The system prompt + user message are NEVER counted
against or truncated by this budget. A ``context_truncated`` audit step is written iff any
item was dropped.
"""

from __future__ import annotations

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_CONTEXT_TRUNCATED
from ..config import get_settings
from ..pipeline import PipelineContext, Stage


class PromptBuildStage(Stage):
    """Build ``ctx.messages`` and splice budgeted RAG/memory/skill context."""

    name = "PROMPT_BUILD"

    async def run(self, ctx: PipelineContext) -> None:
        system_prompt = ctx.agent.system_prompt if ctx.agent is not None else ""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Splice enhancement context (budgeted) between the system prompt and the user turn.
        context_block, dropped = self._build_context(ctx)
        if context_block:
            messages.append({"role": "system", "content": context_block})

        messages.append({"role": "user", "content": ctx.prompt_text})
        ctx.messages = messages

        # Record a context_truncated step ONLY when the budget actually dropped something.
        if dropped:
            await self._record_truncation(ctx, dropped)

    # ── context assembly + budget ──────────────────────────────────────────────────
    def _build_context(self, ctx: PipelineContext) -> tuple[str, dict[str, int]]:
        """Assemble the budgeted context block; return (block_text, dropped-counts).

        Returns ``("", {})`` when there is no enhancement context — the base two-message
        prompt is then byte-identical to the first cycle.
        """
        # Prefer SKILL_LOAD's resolved skills (name + description, access-gated); fall back
        # to the agent's configured skill names when SKILL_LOAD did not run.
        if ctx.skills:
            skills = [self._fmt_skill(s) for s in ctx.skills]
        elif ctx.agent is not None:
            skills = list(ctx.agent.allowed_skills)
        else:
            skills = []
        rag = ctx.rag_chunks
        memories = ctx.memories
        if not rag and not memories and not skills:
            return "", {}

        settings = get_settings()
        token_budget = ctx.agent.token_budget_per_task if ctx.agent is not None else 0
        char_budget = int(
            token_budget * settings.prompt_context_budget_fraction * settings.prompt_context_chars_per_token
        )

        # Render each section's items to text lines (kept aligned with the source lists so a
        # truncation drops whole items off the END of a section).
        rag_lines = [f"- [{c.get('kb_id', '')}] {c.get('text', '')}" for c in rag]
        mem_lines = [f"- {m.get('content', '')}" for m in memories]
        skill_lines = [f"- {s}" for s in skills]

        # Truncate in order RAG -> memory -> skills until the whole block fits the budget.
        dropped = {"rag": 0, "memory": 0, "skills": 0}
        sections = [("rag", rag_lines), ("memory", mem_lines), ("skills", skill_lines)]
        while self._block_chars(sections) > char_budget and any(lines for _k, lines in sections):
            for key, lines in sections:  # RAG first, then memory, then skills
                if lines:
                    lines.pop()  # drop the lowest-ranked item of the highest-priority-to-drop section
                    dropped[key] += 1
                    break

        block = self._render_block(sections)
        dropped = {k: v for k, v in dropped.items() if v > 0}
        return block, dropped

    @staticmethod
    def _fmt_skill(skill: dict[str, str]) -> str:
        """Render a SKILL_LOAD-resolved skill as ``name — description`` (name-only if no desc)."""
        name = str(skill.get("name", "")).strip()
        desc = str(skill.get("description", "")).strip()
        return f"{name} — {desc}" if desc else name

    @staticmethod
    def _block_chars(sections: list[tuple[str, list[str]]]) -> int:
        return sum(len(line) + 1 for _k, lines in sections for line in lines)

    @staticmethod
    def _render_block(sections: list[tuple[str, list[str]]]) -> str:
        headers = {
            "rag": "Relevant knowledge-base context:",
            "memory": "Relevant memories:",
            "skills": "Available skills:",
        }
        parts: list[str] = []
        for key, lines in sections:
            if lines:
                parts.append(headers[key] + "\n" + "\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    async def _record_truncation(ctx: PipelineContext, dropped: dict[str, int]) -> None:
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_CONTEXT_TRUNCATED,
                step_name="context_truncated",
                status="passed",
                duration_ms=0,
                output={"dropped": dropped},
            ),
        )
