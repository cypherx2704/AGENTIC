"""TOOL_LOOP stage — the iterative LLM<->tool loop (Component 7, WP12).

Runs AFTER the base LLM stage (registry slot ``TOOL_LOOP``). TRIGGER: registry-disabled by
default; even when ``STAGE_ENABLE_TOOL_LOOP`` is on, the stage SKIPS unless (a) the agent's
runtime config lists ``allowed_tools`` AND (b) the agent's per-agent ``tool_loop_enabled``
toggle is true (migration 0007, default true). So a toolless agent — OR an agent switched to
"per request" mode (``tool_loop_enabled=false``) — carries no tool behaviour and makes a
single LLM call (the base LLM answer stands). "Per request" mode is for rate-limited /
free-tier models where multiple LLM<->tool round-trips exhaust the shared usage limit.

BEHAVIOUR (only when the agent has allowed_tools):
  1. Resolve EACH allowed tool via the Tool Registry with VERSION-PIN enforcement — an
     entry ``name@version`` pins the version (only that version is resolvable/invokable);
     a bare ``name`` resolves ``latest``. A tool that fails to resolve is dropped from the
     offered set (logged) — the loop proceeds with the tools that did resolve.
  2. Offer the resolved tool schemas to the LLM and run the loop: the model proposes tool
     calls -> dispatch each via ``McpClient.invoke_mcp`` (real MCP: initialize -> tools/call;
     Idempotency-Key = task_id:tool_call_id; the client owns the retry/breaker; retries ONLY
     conn/5xx, never 4xx) -> feed each
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

MODEL MISBEHAVIOUR (a weak model is assumed, not treated as an anomaly):
  * A tool call naming a tool that was NEVER OFFERED (a hallucinated ``brave_search`` /
    ``web_search``) is dropped from the assistant turn and answered with a plain-dialogue
    correction. It must never reach the message history: providers validate the history's
    tool_calls against the request's ``tools[]`` and reject the entire NEXT request when one is
    missing ("attempted to call tool 'brave_search' which was not in request.tools"), turning one
    invented name into a dead task.
  * A tool call the PROVIDER cannot parse (Groq 400 ``tool_use_failed``) is retried ONCE in the
    gateway's EMULATED tool mode, which takes ``tools[]`` off the wire entirely and parses +
    allow-lists the reply itself — so the provider's tool parser is never involved. The task then
    stays in emulated mode. Only if that also fails is the task terminal.
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

#: The gateway's emulated tool-calling mode: tools[] is stripped off the wire request, the protocol
#: is taught in the prompt, and the gateway parses + allow-lists the reply itself.
_TOOL_MODE_EMULATED = "emulated"

#: Provider error code for "the model's tool call could not be parsed/validated" (Groq's
#: ``tool_use_failed``). This is a MODEL failure, not a malformed request from us — retrying it
#: natively just reproduces it, so it is the one LLM error the loop recovers from (via emulation).
_PROVIDER_CODE_TOOL_USE_FAILED = "tool_use_failed"


class _ResolvedTool:
    """A resolved, allowed tool ready to offer to the LLM and invoke.

    MCP naming (Contract 4) distinguishes TWO identifiers, and conflating them is a bug:

      * ``server_name`` — the MCP SERVER / registry name (dash-case, e.g. ``tool-web-search``).
        This is the key the Tool Registry catalogs the server under (``GET /v1/tools/{server_name}``)
        and is used ONLY for resolution + logging provenance.
      * ``tool_name``   — the TOOL / capability name (snake_case, e.g. ``web_search``) declared in
        ``manifest.tools[].name``. A single MCP server may host MANY tools; the ``tools/call``
        ``name`` selects WHICH one. This is what the LLM is offered as the function name AND what is
        sent as the ``name`` in ``tools/call`` — the server rejects any other value.

    ``name`` is retained as an alias of ``tool_name`` so existing call sites (``by_name`` dispatch,
    ``_select_tools`` scoring) keep working unchanged.
    """

    __slots__ = ("server_name", "tool_name", "version", "invoke_url", "mcp_endpoint", "schema")

    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        version: str,
        invoke_url: str,
        schema: dict[str, Any],
        mcp_endpoint: str | None = None,
    ) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.version = version
        self.invoke_url = invoke_url
        # Full real-MCP (JSON-RPC/Streamable-HTTP) URL, ``{invoke_url}{mcp.endpoint}``, from the
        # server's ``mcp`` manifest descriptor. Always set — a server without it is skipped upstream.
        self.mcp_endpoint = mcp_endpoint
        self.schema = schema

    @property
    def name(self) -> str:
        """The tool (capability) name — what the LLM calls and what invoke sends as ``tool``."""
        return self.tool_name


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
        # RUN-level tool switch (the caller's "Use Tools" choice). OFF => this task is a plain chat
        # completion: no tool is resolved, offered to the model, or invoked. Deliberately checked
        # BEFORE resolution so a tools-off run makes zero Tool-Registry / MCP calls — "off" must mean
        # the tools are not merely unused but unreachable. Independent of the agent-level toggle
        # below: either switch alone vetoes.
        if not ctx.use_tools:
            logger.info(
                "tool_loop_disabled_for_run",
                task_id=ctx.task.task_id,
                agent_id=agent.agent_id,
            )
            return
        # Per-agent tool-loop toggle (migration 0007): "per request" mode. When disabled the
        # stage SKIPS even with allowed_tools, so the task makes a single LLM call (the base
        # LLM answer stands) — for rate-limited / free-tier models where multiple round-trips
        # exhaust the provider's shared usage limit. Default true => the loop runs as before.
        if not getattr(agent, "tool_loop_enabled", True):
            logger.info(
                "tool_loop_disabled_for_agent",
                task_id=ctx.task.task_id,
                agent_id=agent.agent_id,
            )
            return

        settings = get_settings()
        resolved = await self._resolve_tools(ctx, agent.allowed_tools)
        if not resolved:
            logger.info("tool_loop_no_tools_resolved", task_id=ctx.task.task_id)
            return  # nothing resolved -> the base LLM answer stands

        # Deterministic dispatch: two DISTINCT servers in allowed_tools can each declare a tool
        # with the SAME capability name (e.g. a retired 'tool-web-search' and its flow-tool
        # replacement both expose 'web_search'). Offering two identically-named function schemas
        # is ambiguous and the by_name map would silently collapse them (last-wins). Keep the
        # FIRST (allowed_tools order = the agent's declared preference) and drop later collisions.
        resolved = self._dedupe_by_tool_name(ctx, resolved)

        # Small-model robustness: offer only the top-N most relevant tools so a weak (8B)
        # model has a small decision space. The gateway separately EMULATES tool-calling
        # for non-native models, so the loop below is identical for small and large models.
        resolved = self._select_tools(resolved, ctx.prompt_text, settings)
        by_name = {t.name: t for t in resolved}
        tool_schemas = [t.schema for t in resolved]
        llms = deps.get_llms_client()
        mcp = deps.get_mcp_client()
        messages: list[dict[str, Any]] = self._loop_messages(ctx, settings, sorted(by_name))

        # Tool-calling mode for this task. A provider that rejects the NATIVE tool protocol
        # downgrades it to emulated for every remaining turn (see _chat).
        tool_mode = settings.tool_loop_tool_mode

        for _iteration in range(settings.tool_loop_max_iterations):
            try:
                completion, tool_mode = await self._chat(
                    ctx, llms, agent, messages, tool_schemas, tool_mode
                )
            except ApiError as exc:
                # The round-trip failed and could not be recovered — terminal for the task (the
                # base LLM answer, if any, is left as-is; mark failed so EVENT records it).
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

            # Split the requests into tools we actually OFFERED and tools we did not. A weak model
            # readily invents a familiar-sounding tool it was never given (``brave_search``,
            # ``web_search``). Such a call must NEVER enter the message history: a provider
            # validates every tool_call in the history against the request's ``tools[]`` and
            # rejects the whole NEXT request when one is absent ("attempted to call tool
            # 'brave_search' which was not in request.tools") — one hallucinated name then kills
            # the task. Drop them from the assistant turn and correct the model in plain dialogue.
            offered = [c for c in completion.tool_calls if c.name in by_name]
            unoffered = [c for c in completion.tool_calls if c.name not in by_name]

            if offered:
                # The assistant turn carries ONLY dispatchable calls, each answered by a tool result.
                messages.append(self._assistant_turn(completion, offered))
                for call in offered:
                    # Multi-call budget: a HARD cap on total invocations across the task.
                    if ctx.tool_invocations >= settings.tool_loop_max_invocations:
                        self._fail_budget(ctx, "tool invocation budget exceeded")
                        return

                    await self._invoke_one(ctx, mcp, by_name[call.name], call, messages, settings)
                    if ctx.terminal_error is not None:
                        return

            if unoffered:
                names = sorted({c.name for c in unoffered})
                logger.info(
                    "tool_loop_unoffered_call_dropped",
                    task_id=ctx.task.task_id,
                    requested=names,
                    offered=sorted(by_name),
                )
                for call in unoffered:
                    await self._record_tool_step(
                        ctx, call.name, None, "failed", 0, error="tool_not_allowed"
                    )
                messages.append(self._unoffered_tools_message(names, sorted(by_name)))

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
    async def _chat(
        self,
        ctx: PipelineContext,
        llms: Any,
        agent: Any,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        tool_mode: str | None,
    ) -> tuple[Any, str | None]:
        """Run ONE loop turn, falling back from native to EMULATED tool-calling at most once.

        A model that is weak at the provider's native function-calling protocol can emit a call the
        PROVIDER itself refuses to parse (Groq answers 400 ``tool_use_failed``). That is a failure
        of the MODEL, not a malformed request from us, so re-issuing it natively only reproduces it
        — while the gateway's emulated mode cannot hit it at all: it strips ``tools[]`` off the wire
        request (so the provider never runs its tool parser), teaches the protocol in the prompt,
        flattens the tool history, and parses + allow-lists the reply itself.

        The fallback mode is RETURNED so the caller keeps it for the remaining turns — otherwise
        every iteration would pay for one more failed native call. Any other error, and any failure
        that survives the fallback, propagates to the caller (terminal).
        """
        try:
            completion = await self._call_llm(llms, agent, ctx, messages, tool_schemas, tool_mode)
            return completion, tool_mode
        except ApiError as exc:
            if tool_mode == _TOOL_MODE_EMULATED or not self._is_tool_protocol_failure(exc):
                raise
            logger.warning(
                "tool_loop_native_tools_rejected",
                task_id=ctx.task.task_id,
                error=exc.message,
                fallback=_TOOL_MODE_EMULATED,
            )
        completion = await self._call_llm(
            llms, agent, ctx, messages, tool_schemas, _TOOL_MODE_EMULATED
        )
        return completion, _TOOL_MODE_EMULATED

    @staticmethod
    async def _call_llm(
        llms: Any,
        agent: Any,
        ctx: PipelineContext,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        tool_mode: str | None,
    ) -> Any:
        """One gateway chat call for the loop (tools offered, identity in headers)."""
        return await llms.chat(
            model=agent.llm_model,
            messages=messages,
            max_tokens=agent.effective_max_tokens(),
            temperature=agent.temperature,
            tools=tool_schemas,
            tool_mode=tool_mode,
            agent_jwt=ctx.inbound_agent_jwt,
            on_behalf_of=ctx.principal.agent_id,
        )

    @staticmethod
    def _is_tool_protocol_failure(exc: ApiError) -> bool:
        """True when the PROVIDER rejected the model's tool call as unparsable/invalid.

        Keyed on the PROVIDER's own code (the gateway preserves it at ``details.provider_code``),
        never on the gateway's code: every provider 400 flattens to ``VALIDATION_ERROR`` there, and
        that cannot tell a genuinely bad request (do not retry) from a model that fumbled the tool
        protocol (retry differently). Unknown/absent code => not recoverable.
        """
        details = getattr(exc, "details", None) or {}
        return str(details.get("provider_code") or "").lower() == _PROVIDER_CODE_TOOL_USE_FAILED

    @staticmethod
    def _unoffered_tools_message(requested: list[str], offered: list[str]) -> dict[str, Any]:
        """Correct a model that called a tool it was never given — as PLAIN dialogue.

        Deliberately NOT a ``tool`` message: a tool result must answer a tool_call present in the
        preceding assistant turn, and this call was dropped from that turn precisely because the
        provider would reject the history for containing it. A plain user turn keeps the history
        valid on every provider while still telling the model what it did wrong and what it may
        actually call.
        """
        names = ", ".join(requested)
        available = ", ".join(offered) if offered else "none"
        return {
            "role": "user",
            "content": (
                f"There is no tool named {names}. Do not call it again. "
                f"The only tools available to you are: {available}. "
                "Call one of those, or answer directly from what you already know."
            ),
        }

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
            server_name = res.name or name
            mcp_endpoint = self._mcp_endpoint_of(res.invoke_url, res.manifest)
            if mcp_endpoint is None:
                # MCP is the only tool wire — a server that does not advertise an ``mcp``
                # descriptor cannot be invoked, so it is not offered to the model.
                logger.warning("tool_no_mcp_endpoint", task_id=ctx.task.task_id, tool=server_name)
                continue
            # An MCP server declares its tools in ``manifest.tools[]`` (Contract 4). Offer EACH of
            # them to the LLM under its OWN tool name; ``tools/call`` selects which one by name.
            for tool_name, tool_schema in self._tools_of(res.manifest):
                resolved.append(
                    _ResolvedTool(
                        server_name=server_name,
                        tool_name=tool_name,
                        version=res.version,
                        invoke_url=res.invoke_url,
                        schema=tool_schema,
                        mcp_endpoint=mcp_endpoint,
                    )
                )
        return resolved

    @staticmethod
    def _mcp_endpoint_of(invoke_url: str, manifest: dict[str, Any]) -> str | None:
        """Resolve the tool server's real-MCP URL from its manifest ``mcp`` descriptor.

        Returns ``{invoke_url}{mcp.endpoint}`` when the manifest advertises a real-MCP
        (Streamable-HTTP) transport, else ``None`` (a server that does not speak MCP — it is
        skipped, since MCP is the only tool wire). ``invoke_url`` is the registry-resolved base.
        """
        mcp = manifest.get("mcp") if isinstance(manifest, dict) else None
        if not isinstance(mcp, dict):
            return None
        endpoint = mcp.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        return f"{invoke_url.rstrip('/')}{endpoint}"

    @staticmethod
    def _tools_of(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Yield ``(tool_name, llm_schema)`` for every tool an MCP server declares.

        One entry per ``manifest.tools[]`` element (Contract 4), using its OWN ``name`` (e.g.
        ``web_search``) — NOT the server name. A manifest with no usable ``tools[]`` offers
        nothing; MCP servers always declare their tools, so there is no single-tool fallback.
        """
        entries: list[tuple[str, dict[str, Any]]] = []
        tools = manifest.get("tools")
        if isinstance(tools, list):
            for t in tools:
                if not isinstance(t, dict):
                    continue
                tname = str(t.get("name", "") or "").strip()
                if not tname:
                    continue
                entries.append((tname, ToolLoopStage._schema_for(tname, t)))
        return entries

    @staticmethod
    def _dedupe_by_tool_name(ctx: PipelineContext, resolved: list[_ResolvedTool]) -> list[_ResolvedTool]:
        """Keep the FIRST resolved tool per tool-name; drop later same-named collisions (logged).

        The tool NAME (capability) is what the LLM is offered and what ``tools/call`` selects, so two
        servers exposing the same name are indistinguishable to the model and to the ``by_name``
        dispatch map. Preserving the first (allowed_tools order = the agent's declared preference)
        makes both the offered schema list and dispatch deterministic instead of last-wins.
        """
        seen: dict[str, _ResolvedTool] = {}
        deduped: list[_ResolvedTool] = []
        for tool in resolved:
            kept = seen.get(tool.name)
            if kept is not None:
                logger.warning(
                    "tool_loop_duplicate_tool_name_dropped",
                    task_id=ctx.task.task_id,
                    tool_name=tool.name,
                    kept_server=kept.server_name,
                    dropped_server=tool.server_name,
                )
                continue
            seen[tool.name] = tool
            deduped.append(tool)
        return deduped

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
    def _loop_messages(
        ctx: PipelineContext, settings: Any, tool_names: list[str]
    ) -> list[dict[str, Any]]:
        """Copy the prompt messages, optionally prepending a 'use tools' system nudge.

        The nudge is inserted AFTER any leading system messages (so the agent's system prompt stays
        first) and helps weaker models actually invoke an available tool instead of answering from
        priors. Gated by ``tool_loop_tool_use_nudge``.

        It NAMES the offered tools and states that the list is exhaustive. A weak model that is only
        told "you have tools" reaches for whatever tool it remembers from training — it invented
        ``brave_search`` here, a tool no agent in this tenant holds — and a provider then rejects the
        whole request for calling a tool that was never offered. Naming the set, and saying plainly
        that nothing else exists, removes the guess. The complementary half is the instruction NOT to
        fabricate when the list cannot get what the step needs: a model denied its imagined search
        tool will otherwise answer from priors and present it as fact.
        """
        messages: list[dict[str, Any]] = list(ctx.messages)
        if not getattr(settings, "tool_loop_tool_use_nudge", False):
            return messages
        offered = ", ".join(tool_names) if tool_names else "none"
        nudge = {
            "role": "system",
            "content": (
                f"You have exactly these tools, and no others: {offered}. "
                "Call one when it would make your answer more accurate, or when the request cannot "
                "be satisfied without it; otherwise answer directly. "
                "That list is complete: never call, invent, or guess any other tool name — no web "
                "search, no browser, no code interpreter, unless it is named above. "
                "If none of them can get what you need, say so plainly and answer from what you "
                "already know. Never invent a tool's output or present a guess as a fact you looked up."
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
            # Dispatch over real MCP (JSON-RPC / Streamable HTTP): initialize -> tools/call,
            # with auth + Idempotency-Key + per-(endpoint, agent) circuit breaker + conn/5xx retry.
            result = await mcp.invoke_mcp(
                tool.mcp_endpoint,
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
            # The registry catalogs access by the SERVER name (its resource path); the CAPABILITY
            # is the tool name the model called. Using tool.name here would 404 the registry
            # (it knows the server as ``tool-web-search``, not the tool ``web_search``) and
            # fail-close to 'none' — so pass the server name for the lookup, tool name as capability.
            return await get_access(
                tool.server_name,
                capability=call.name,
                agent_jwt=ctx.inbound_agent_jwt,
                on_behalf_of=ctx.principal.agent_id,
            )
        except ApiError as exc:
            logger.warning(
                "tool_access_resolve_failed",
                task_id=ctx.task.task_id,
                tool=tool.name,
                error=exc.message,
            )
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
    def _assistant_turn(completion: Any, calls: list[Any]) -> dict[str, Any]:
        """Build the assistant message carrying the model's tool-call requests.

        Only ``calls`` — the OFFERED, dispatchable subset — is included. Reuses the gateway's raw
        ``message.tool_calls`` block for those ids when present (so the ids line up with what the
        provider issued), else reconstructs the OpenAI tool-call shape from the parsed
        :class:`ToolCall` list.

        A call naming a tool we never offered is deliberately EXCLUDED: providers validate the
        history's tool_calls against the request's ``tools[]`` and reject the entire request when
        one is missing, so admitting it here would poison every subsequent turn of the loop.
        """
        keep = {c.id for c in calls}
        raw_choices = completion.raw.get("choices") if isinstance(completion.raw, dict) else None
        raw_message = raw_choices[0].get("message", {}) if raw_choices else {}
        raw_tool_calls = raw_message.get("tool_calls") if isinstance(raw_message, dict) else None
        kept_raw = (
            [tc for tc in raw_tool_calls if isinstance(tc, dict) and tc.get("id") in keep]
            if raw_tool_calls
            else []
        )
        if not kept_raw:
            kept_raw = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments, default=str)},
                }
                for c in calls
            ]
        return {"role": "assistant", "content": completion.content or "", "tool_calls": kept_raw}

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
