"""TOOL_LOOP stage — the iterative LLM<->tool loop (Component 7, WP12).

Runs AFTER the base LLM stage (registry slot ``TOOL_LOOP``). TRIGGER: registry-disabled by
default; even when ``STAGE_ENABLE_TOOL_LOOP`` is on, the stage SKIPS unless the agent's
runtime config lists ``allowed_tools``. So a toolless agent carries no tool behaviour
regardless of the flag (and the base LLM answer stands).

BEHAVIOUR (only when the agent has allowed_tools):
  1. Resolve EACH allowed tool via the Tool Registry with VERSION-PIN enforcement — an
     entry ``name@version`` pins the version (only that version is resolvable/invokable);
     a bare ``name`` resolves ``latest``. A tool that fails to resolve is dropped from the
     offered set (logged) — the loop proceeds with the tools that did resolve.
  2. Offer the resolved tool schemas to the LLM and run the loop: the model proposes tool
     calls -> dispatch each via ``McpClient.invoke`` (Idempotency-Key = task_id:tool_call_id,
     the client owns the retry/breaker; retries ONLY conn/5xx, never 4xx) -> feed each
     result back as a ``tool`` message -> ask the model again. Repeat until the model stops
     requesting tools (final answer) or a bound is hit.

BOUNDS / accounting:
  * ``settings.tool_loop_max_iterations`` (10) — after this many LLM turns that still
    request tools, STOP and record a ``tool_loop_limit`` step (the partial answer is
    returned, NOT an error).
  * ``settings.tool_loop_max_invocations`` — the multi-call budget across the whole task;
    crossing it short-circuits ``BUDGET_EXCEEDED`` (a terminal error -> EVENT marks failed).
  * ``ctx.cost_budget_usd`` — if set, the per-turn LLM cost + (gateway-reported) cost is
    accrued and the loop short-circuits ``BUDGET_EXCEEDED`` before exceeding it.
  * One ``...tools.invocation.metered`` outbox event is emitted PER successful or failed
    invocation (the metering signal), and one ``tool_call`` audit step per invocation.

A failed tool invocation is FAIL-SOFT to the LOOP (its error is fed back to the model so it
can recover / answer without the tool), not fatal to the task — except the hard budget caps
above, which ARE terminal.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog

from ...db import outbox, steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_TOOL_CALL, STEP_TYPE_TOOL_LOOP_LIMIT
from ..config import get_settings
from ..errors import ApiError, ErrorCode
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)


class _ResolvedTool:
    """A resolved, allowed tool: its invoke URL, version, and the schema offered to the LLM."""

    __slots__ = ("name", "version", "invoke_url", "schema")

    def __init__(self, name: str, version: str, invoke_url: str, schema: dict[str, Any]) -> None:
        self.name = name
        self.version = version
        self.invoke_url = invoke_url
        self.schema = schema


def _split_pin(entry: str) -> tuple[str, str | None]:
    """Split an ``allowed_tools`` entry ``name`` | ``name@version`` into (name, version)."""
    if "@" in entry:
        name, _, version = entry.partition("@")
        return name.strip(), (version.strip() or None)
    return entry.strip(), None


class ToolLoopStage(Stage):
    """Resolve allowed tools (version-pinned) and run the bounded LLM<->tool loop."""

    name = "TOOL_LOOP"

    async def run(self, ctx: PipelineContext) -> None:
        agent = ctx.agent
        if agent is None or not agent.allowed_tools:
            return  # no tools configured -> the base LLM answer stands (default-disabled shape)

        settings = get_settings()
        resolved = await self._resolve_tools(ctx, agent.allowed_tools)
        if not resolved:
            logger.info("tool_loop_no_tools_resolved", task_id=ctx.task.task_id)
            return  # nothing resolved -> the base LLM answer stands

        # Small-model robustness: offer only the top-N most relevant tools so a weak (8B)
        # model has a small decision space. The gateway separately EMULATES tool-calling
        # for non-native models, so the loop below is identical for small and large models.
        resolved = self._select_tools(resolved, ctx.prompt_text, settings)
        by_name = {t.name: t for t in resolved}
        tool_schemas = [t.schema for t in resolved]
        llms = deps.get_llms_client()
        mcp = deps.get_mcp_client()
        messages: list[dict[str, Any]] = self._loop_messages(ctx, settings)

        for _iteration in range(settings.tool_loop_max_iterations):
            try:
                completion = await llms.chat(
                    model=agent.llm_model,
                    messages=messages,
                    max_tokens=agent.effective_max_tokens(),
                    temperature=agent.temperature,
                    tools=tool_schemas,
                    tool_mode=settings.tool_loop_tool_mode,
                    agent_jwt=ctx.inbound_agent_jwt,
                    on_behalf_of=ctx.principal.agent_id,
                )
            except ApiError as exc:
                # A loop LLM round-trip failed — terminal for the task (the base LLM answer,
                # if any, is left as-is; mark failed so EVENT records it).
                logger.warning("tool_loop_llm_failed", task_id=ctx.task.task_id, error=exc.message)
                ctx.fail(exc.code, exc.message, status="failed")
                return

            ctx.tokens_used += completion.usage.total_tokens
            ctx.cost_usd += completion.usage.cost_usd
            if self._cost_exceeded(ctx):
                self._fail_budget(ctx, "cost budget exceeded during tool loop")
                return

            # No tool calls -> the model produced its final answer.
            if not completion.tool_calls:
                if completion.content is not None:
                    ctx.final_answer = completion.content
                return

            # Append the assistant turn (carrying the tool-call requests) before dispatch.
            messages.append(self._assistant_turn(completion))

            for call in completion.tool_calls:
                # Multi-call budget: a HARD cap on total invocations across the task.
                if ctx.tool_invocations >= settings.tool_loop_max_invocations:
                    self._fail_budget(ctx, "tool invocation budget exceeded")
                    return

                tool = by_name.get(call.name)
                if tool is None:
                    # The model asked for a tool that is not allowed/resolved — feed back an
                    # error so it can recover; do NOT invoke (version-pin / allow-list guard).
                    messages.append(self._tool_message(call.id, call.name, {"error": "tool_not_allowed"}))
                    await self._record_tool_step(ctx, call.name, None, "failed", 0, error="tool_not_allowed")
                    continue

                await self._invoke_one(ctx, mcp, tool, call, messages, settings)
                if ctx.terminal_error is not None:
                    return

        # Exhausted the iteration budget while the model still wanted tools — STOP with the
        # partial answer and record a tool_loop_limit step (NOT an error).
        logger.info(
            "tool_loop_limit_reached",
            task_id=ctx.task.task_id,
            iterations=settings.tool_loop_max_iterations,
        )
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_TOOL_LOOP_LIMIT,
                step_name="tool_loop_limit",
                status="passed",
                duration_ms=0,
                output={
                    "max_iterations": settings.tool_loop_max_iterations,
                    "tool_invocations": ctx.tool_invocations,
                },
            ),
        )

    # ── helpers ────────────────────────────────────────────────────────────────────
    async def _resolve_tools(self, ctx: PipelineContext, allowed: list[str]) -> list[_ResolvedTool]:
        """Resolve each allowed tool with version-pin enforcement; drop the unresolvable."""
        registry = deps.get_registry_client()
        resolved: list[_ResolvedTool] = []
        for entry in allowed:
            name, pinned = _split_pin(entry)
            if not name:
                continue
            try:
                res = await registry.resolve_tool(
                    name,
                    pinned,
                    agent_jwt=ctx.inbound_agent_jwt,
                    on_behalf_of=ctx.principal.agent_id,
                )
            except ApiError as exc:
                logger.warning("tool_resolve_failed", task_id=ctx.task.task_id, tool=entry, error=exc.message)
                continue
            # Version-pin enforcement: a pinned entry MUST match the resolved version exactly.
            if pinned is not None and res.version and res.version != pinned:
                logger.warning(
                    "tool_version_pin_mismatch",
                    task_id=ctx.task.task_id,
                    tool=name,
                    pinned=pinned,
                    resolved=res.version,
                )
                continue
            resolved.append(
                _ResolvedTool(
                    name=res.name or name,
                    version=res.version,
                    invoke_url=res.invoke_url,
                    schema=self._schema_for(res.name or name, res.manifest),
                )
            )
        return resolved

    @staticmethod
    def _select_tools(
        resolved: list[_ResolvedTool], prompt_text: str, settings: Any
    ) -> list[_ResolvedTool]:
        """Offer only the top-N tools most relevant to the user message (small-model focus).

        Ranks by lexical overlap of the user message against each tool's name + description
        (cheap, dependency-free) and keeps the highest-scoring ``tool_loop_max_offered_tools``
        (0 = no cap). Stable for ties (original order preserved), so a no-overlap prompt just
        takes the first N. A large model is unaffected when the cap exceeds the tool count.
        """
        cap = getattr(settings, "tool_loop_max_offered_tools", 0)
        if cap <= 0 or len(resolved) <= cap:
            return resolved
        words = set(re.findall(r"[a-z0-9]+", (prompt_text or "").lower()))

        def score(t: _ResolvedTool) -> int:
            desc = str(t.schema.get("function", {}).get("description", ""))
            toks = set(re.findall(r"[a-z0-9]+", f"{t.name} {desc}".lower()))
            return len(words & toks)

        order = sorted(range(len(resolved)), key=lambda i: (-score(resolved[i]), i))
        return [resolved[i] for i in order[:cap]]

    @staticmethod
    def _loop_messages(ctx: PipelineContext, settings: Any) -> list[dict[str, Any]]:
        """Copy the prompt messages, optionally prepending a 'use tools' system nudge.

        The nudge is inserted AFTER any leading system messages (so the agent's system
        prompt stays first) and helps weaker models actually invoke an available tool
        instead of answering from priors. Gated by ``tool_loop_tool_use_nudge``.
        """
        messages: list[dict[str, Any]] = list(ctx.messages)
        if not getattr(settings, "tool_loop_tool_use_nudge", False):
            return messages
        nudge = {
            "role": "system",
            "content": (
                "You have tools available. When a tool would help you answer more "
                "accurately or act on the request, call it instead of guessing; "
                "otherwise answer directly."
            ),
        }
        idx = 0
        while idx < len(messages) and messages[idx].get("role") == "system":
            idx += 1
        messages.insert(idx, nudge)
        return messages

    @staticmethod
    def _schema_for(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Build the LLM-facing tool schema from the registry manifest (OpenAI tool shape)."""
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": str(manifest.get("description", "")),
                "parameters": (
                    manifest.get("parameters") or manifest.get("input_schema") or {"type": "object"}
                ),
            },
        }

    async def _invoke_one(
        self,
        ctx: PipelineContext,
        mcp: Any,
        tool: _ResolvedTool,
        call: Any,
        messages: list[dict[str, Any]],
        settings: Any,
    ) -> None:
        """Invoke one tool call: enforce access (none/ask/automated), meter, record, feed back."""
        # ── Access control (Phase 5/6) — resolve the agent's mode for this tool+capability ──
        access_mode = await self._resolve_access(ctx, tool, call)
        if access_mode == "none":
            messages.append(self._tool_message(call.id, call.name, {"error": "tool_access_denied"}))
            await self._record_tool_step(
                ctx, tool.name, tool.version, "failed", 0, error="tool_access_denied"
            )
            logger.info("tool_access_denied", task_id=ctx.task.task_id, tool=tool.name)
            return
        if access_mode == "ask":
            approved = await self._await_approval(ctx, tool, call)
            if not approved:
                messages.append(
                    self._tool_message(call.id, call.name, {"error": "tool_approval_denied"})
                )
                await self._record_tool_step(
                    ctx, tool.name, tool.version, "failed", 0, error="tool_approval_denied"
                )
                return

        started = time.monotonic()
        ctx.tool_invocations += 1
        tool_call_id = call.id or f"{ctx.task.task_id}:{ctx.tool_invocations}"
        status = "passed"
        outcome: dict[str, Any]
        try:
            result = await mcp.invoke(
                tool.invoke_url,
                call.name,
                call.arguments,
                task_id=ctx.task.task_id,
                tool_call_id=tool_call_id,
                agent_jwt=ctx.inbound_agent_jwt,
                on_behalf_of=ctx.principal.agent_id,
            )
            outcome = {"result": result.result}
            ctx.tool_results.append(
                {"tool": tool.name, "tool_call_id": tool_call_id, "result": result.result}
            )
        except ApiError as exc:
            # FAIL-SOFT to the loop: feed the error back so the model can recover/answer.
            status = "failed"
            outcome = {"error": exc.code, "message": exc.message}
            ctx.tool_results.append({"tool": tool.name, "tool_call_id": tool_call_id, "error": exc.code})
            logger.warning("tool_invoke_failed", task_id=ctx.task.task_id, tool=tool.name, error=exc.message)

        messages.append(self._tool_message(tool_call_id, tool.name, outcome))
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._emit_metered(ctx, tool, tool_call_id, status, settings)
        await self._record_tool_step(
            ctx, tool.name, tool.version, status, duration_ms, tool_call_id=tool_call_id
        )

    async def _resolve_access(self, ctx: PipelineContext, tool: _ResolvedTool, call: Any) -> str:
        """Resolve the agent's access mode (none|ask|automated) for this tool + capability.

        FAIL-CLOSED: a registry error returns ``none`` (a tool whose access can't be confirmed is
        not invoked). The capability is the tool-call's function name (snake_case MCP capability).
        A registry without the access-control surface (a legacy client) is treated as unrestricted —
        the real RegistryClient always exposes ``get_tool_access``, so production enforcement holds.
        """
        registry = deps.get_registry_client()
        get_access = getattr(registry, "get_tool_access", None)
        if get_access is None:
            return "automated"
        try:
            return await get_access(
                tool.name,
                capability=call.name,
                agent_jwt=ctx.inbound_agent_jwt,
                on_behalf_of=ctx.principal.agent_id,
            )
        except ApiError as exc:
            logger.warning("tool_access_resolve_failed", task_id=ctx.task.task_id, tool=tool.name, error=exc.message)
            return "none"

    async def _await_approval(self, ctx: PipelineContext, tool: _ResolvedTool, call: Any) -> bool:
        """Human-in-the-loop gate for an ``ask``-mode tool (Phase 6).

        Requests an approval at Auth and polls until granted/denied/timeout. When the HIL client
        is unwired (e.g. tests, or HIL not configured) this DENIES by default — an ``ask`` tool must
        never auto-run without an explicit approval path.
        """
        hil = deps.get_hil_client_optional()
        if hil is None:
            logger.info("tool_hil_unavailable_denied", task_id=ctx.task.task_id, tool=tool.name)
            return False
        return await hil.request_and_wait(
            ctx,
            operation_type="tool_execution",
            context={
                "tool": tool.name,
                "tool_version": tool.version,
                "capability": call.name,
                "task_id": ctx.task.task_id,
            },
        )

    @staticmethod
    def _assistant_turn(completion: Any) -> dict[str, Any]:
        """Build the assistant message carrying the model's tool-call requests.

        Reuses the gateway's raw ``message.tool_calls`` block verbatim when present (so the
        downstream tool ids line up), else reconstructs the OpenAI tool-call shape from the
        parsed :class:`ToolCall` list.
        """
        raw_choices = completion.raw.get("choices") if isinstance(completion.raw, dict) else None
        raw_message = raw_choices[0].get("message", {}) if raw_choices else {}
        raw_tool_calls = raw_message.get("tool_calls") if isinstance(raw_message, dict) else None
        if not raw_tool_calls:
            raw_tool_calls = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments, default=str)},
                }
                for c in completion.tool_calls
            ]
        return {"role": "assistant", "content": completion.content or "", "tool_calls": raw_tool_calls}

    @staticmethod
    def _tool_message(tool_call_id: str, name: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """Build the ``tool`` role message that feeds an invocation result back to the LLM."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": json.dumps(outcome, default=str),
        }

    async def _emit_metered(
        self, ctx: PipelineContext, tool: _ResolvedTool, tool_call_id: str, status: str, settings: Any
    ) -> None:
        """Emit one ``tools.invocation.metered`` outbox event (fail-soft — never fails task)."""
        if ctx.pool is None:
            return
        try:
            await outbox.record_metered_event(
                ctx.pool,
                topic=settings.tool_metering_topic,
                tenant_id=ctx.task.tenant_id,
                trace_id=ctx.trace_id,
                payload={
                    "task_id": ctx.task.task_id,
                    "agent_id": ctx.task.agent_id,
                    "tenant_id": ctx.task.tenant_id,
                    "tool": tool.name,
                    "tool_version": tool.version,
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "trace_id": ctx.trace_id,
                },
                producer_version=settings.service_version,
            )
        except Exception as exc:  # noqa: BLE001 — a metering write must never fail the task
            logger.warning(
                "tool_metered_event_failed", task_id=ctx.task.task_id, tool=tool.name, error=str(exc)
            )

    @staticmethod
    async def _record_tool_step(
        ctx: PipelineContext,
        tool_name: str,
        tool_version: str | None,
        status: str,
        duration_ms: int,
        *,
        tool_call_id: str | None = None,
        error: str | None = None,
    ) -> None:
        output: dict[str, Any] = {"tool": tool_name, "tool_version": tool_version}
        if tool_call_id is not None:
            output["tool_call_id"] = tool_call_id
        if error is not None:
            output["error"] = error
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_TOOL_CALL,
                step_name="tool_call",
                status=status,
                duration_ms=duration_ms,
                output=output,
            ),
        )

    @staticmethod
    def _cost_exceeded(ctx: PipelineContext) -> bool:
        return ctx.cost_budget_usd is not None and ctx.cost_usd > ctx.cost_budget_usd

    @staticmethod
    def _fail_budget(ctx: PipelineContext, reason: str) -> None:
        logger.warning("tool_loop_budget_exceeded", task_id=ctx.task.task_id, reason=reason)
        ctx.fail(ErrorCode.BUDGET_EXCEEDED, reason, status="failed")
