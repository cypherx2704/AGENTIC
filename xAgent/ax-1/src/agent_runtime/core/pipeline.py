"""The stage-pipeline engine (Component 3).

The execution engine is a PIPELINE OF NAMED STAGES, not a procedural function — the
single most important design constraint of Phase 9A (so Tools/Memory/Skills/RAG can be
added later as new stages without re-architecting). This module authors ONLY the engine:

  * :class:`Stage`            — the ABC every concrete stage implements.
  * :class:`PipelineContext`  — the mutable per-task carrier passed to every stage.
  * :class:`Pipeline`         — the runner: executes ENABLED stages in order, short
    -circuits on ``terminal_error``, and ALWAYS runs the EVENT stage last (finally-style).
  * :data:`STAGE_REGISTRY`    — the ordered registry of stage *names* + enabled flags,
    referencing concrete stage classes by name. Enhancement stages
    (MEMORY_RETRIEVE, RAG_QUERY, SKILL_LOAD, TOOL_LOOP, MEMORY_WRITE) are present but
    ``enabled=False`` so the runtime never errors when downstream phases slip
    (Audit Addendum #7).

The CONCRETE stages (LoadStage, PreGuardrailStage, PromptBuildStage, LlmStage,
PostGuardrailStage, EventStage, ...) are authored by the feature agent in a
``stages/`` package, NOT here. The feature agent imports ``Stage`` + ``PipelineContext``
from this module, implements them, and binds the classes into ``STAGE_REGISTRY`` (see
``bind_stage`` / ``build_pipeline`` below) before serving traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from . import metrics
from .errors import ErrorCode

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from ..db.steps_repo import StepBuffer
    from ..db.tasks_repo import TaskRow
    from ..models.agent import AgentRuntime
    from .auth import Principal

logger = structlog.get_logger(__name__)

# The registry name of the in-flight model stage. The runner gives this stage special
# treatment: it races the stage coroutine against a cancel-poll loop so a long-running
# LLM round-trip is cancelled promptly (not only observed BETWEEN stages). A constant —
# it mirrors the LLM slot name in STAGE_REGISTRY, not tunable config.
LLM_STAGE_NAME = "LLM"

# How often the runner re-checks the cancel signal while the LLM stage is in flight
# (seconds). Short enough to abort a hung call quickly, long enough to add negligible
# Valkey load. A runner-internal cadence constant, not a per-deployment tunable.
_CANCEL_POLL_INTERVAL_SECONDS = 0.5


# ── PipelineContext ──────────────────────────────────────────────────────────────
@dataclass
class TerminalError:
    """A fatal error that short-circuits the pipeline (EVENT still runs)."""

    code: str  # Contract 2 code, e.g. GUARDRAIL_VIOLATION | BUDGET_EXCEEDED | INTERNAL_ERROR
    message: str
    # Mapped to the terminal task status: failed | timeout | cancelled.
    status: str = "failed"


@dataclass
class PipelineContext:
    """Mutable per-task carrier threaded through every stage.

    The api layer constructs this, the LOAD stage populates ``agent``, the guardrail/
    LLM stages mutate ``prompt_text`` / ``messages`` / ``final_answer`` and accumulate
    usage, and every user-visible stage appends a :class:`StepRow` to ``steps``. The
    EVENT stage reads the terminal state to finalise the task + emit the Kafka event.
    """

    # ── Identity / correlation (from inbound JWT + trace ctx; NEVER from the body) ──
    principal: Principal
    inbound_agent_jwt: str  # forwarded verbatim as X-Forwarded-Agent-JWT downstream
    trace_id: str
    request_id: str

    # ── Task + config ───────────────────────────────────────────────────────────
    task: TaskRow
    agent: AgentRuntime | None = None

    # ── Prompt assembly + LLM I/O ─────────────────────────────────────────────────
    prompt_text: str = ""  # the user message (mutated by PRE-GUARDRAIL redaction)
    messages: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str | None = None

    # ── Usage accumulators (summed across LLM calls; single call in first cycle) ────
    tokens_used: int = 0
    cost_usd: float = 0.0

    # ── Enhancement-stage context (WP12 — populated by RAG / MEMORY / TOOL stages, ──
    # spliced into the prompt by PROMPT_BUILD). All default-empty so a basic pipeline
    # (no enhancement stages) carries no extra state and PROMPT_BUILD behaves exactly as
    # the first cycle did.
    # RAG chunks stashed by RAG_QUERY: list of {kb_id, chunk_id, text, score, document_id}.
    rag_chunks: list[dict[str, Any]] = field(default_factory=list)
    # Memories stashed by MEMORY_RETRIEVE: list of {id, content, score}.
    memories: list[dict[str, Any]] = field(default_factory=list)
    # Tool results stashed by TOOL_LOOP: list of {tool, tool_call_id, result/error}.
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # Count of tool INVOCATIONS so far (the multi-call budget counter, TOOL_LOOP).
    tool_invocations: int = 0
    # Skills resolved + access-gated by SKILL_LOAD: list of {name, description, access_mode}.
    # PROMPT_BUILD splices these (name + description) as the prompt's "Available skills"
    # context. Empty when SKILL_LOAD did not run -> PROMPT_BUILD falls back to the agent's
    # configured allowed_skills names (byte-identical to the pre-SKILL_LOAD behaviour).
    skills: list[dict[str, Any]] = field(default_factory=list)

    # ── WP12 task parameters threaded from the task row / request body ──────────────
    # Optional conversational-session correlator (scopes session memory; NOT identity).
    session_id: str | None = None
    # Optional per-task USD cost budget — the LLM/tool stages accrue against it and
    # short-circuit BUDGET_EXCEEDED before exceeding it. None = no cost cap.
    cost_budget_usd: float | None = None

    # ── Audit steps (one per user-visible stage; feeds the A2A response too) ────────
    steps: StepBuffer | None = None

    # ── Terminal error (set by any stage to short-circuit; EVENT runs regardless) ──
    terminal_error: TerminalError | None = None

    # ── Shared infra handles (set by the api layer) ────────────────────────────────
    pool: AsyncConnectionPool | None = None
    # Wall-clock start (time.monotonic) for duration_ms accounting.
    started_monotonic: float = 0.0
    # Response timestamps (RFC 3339) — set by the api layer / EVENT stage.
    started_at: str = ""

    # ── Cooperative cancellation (WP08) ────────────────────────────────────────────
    # Optional async predicate the runner polls BETWEEN stages and WHILE the LLM stage
    # is in flight. Returns True iff a cancel was requested (a Valkey cancel signal). It
    # must be FAIL-SAFE: on any error it returns False (the run proceeds; the run never
    # crashes because the cancel store hiccuped). The api layer injects one backed by the
    # Valkey cancel key; tests can inject a simple flag. None disables cancel polling.
    cancel_check: Callable[[], Awaitable[bool]] | None = None

    def fail(self, code: str, message: str, *, status: str = "failed") -> None:
        """Set the terminal error so the runner short-circuits to EVENT."""
        self.terminal_error = TerminalError(code=code, message=message, status=status)

    async def is_cancel_requested(self) -> bool:
        """Poll the injected cancel predicate (fail-safe: False on absence/any error)."""
        if self.cancel_check is None:
            return False
        try:
            return await self.cancel_check()
        except Exception as exc:  # noqa: BLE001 — a cancel-store hiccup must never crash the run
            logger.warning("cancel_check_failed", task_id=self.task.task_id, error=str(exc))
            return False


# ── Stage ABC ──────────────────────────────────────────────────────────────────
class Stage(ABC):
    """One named, independently feature-flagged step of the execution pipeline.

    Concrete stages set the class attribute :attr:`name` and implement :meth:`run`.
    A stage mutates the :class:`PipelineContext` in place; it sets
    ``ctx.terminal_error`` (via ``ctx.fail(...)``) to short-circuit the rest of the
    pipeline. ``run`` should NOT raise for expected failures — set ``terminal_error``
    instead; unexpected exceptions are caught by the runner and converted to an
    ``INTERNAL_ERROR`` terminal error (EVENT still runs).
    """

    #: Stable stage name (matches the STAGE_REGISTRY key + Component 6 step naming).
    name: str = "STAGE"

    #: Whether this stage runs. The registry flips enhancement stages off.
    enabled: bool = True

    @abstractmethod
    async def run(self, ctx: PipelineContext) -> None:
        """Execute the stage against ``ctx`` (mutating it in place)."""
        raise NotImplementedError


# ── Ordered stage registry ──────────────────────────────────────────────────────
@dataclass
class StageSpec:
    """A registry entry: the stage name, default-enabled flag, and (once bound) class."""

    name: str
    enabled: bool
    stage_cls: type[Stage] | None = None  # bound by the feature agent via bind_stage()


# First-cycle order (LOAD -> PRE-GUARDRAIL -> PROMPT_BUILD -> LLM -> POST-GUARDRAIL ->
# EVENT -> RETURN). EVENT is the finally-equivalent stage — the runner runs it last
# even when an earlier stage short-circuits. RETURN is handled by the api layer (it
# serialises the Contract 3 response), so it is NOT a pipeline stage here.
#
# Enhancement stages are listed in their eventual order with enabled=False so the
# pipeline shape is stable and downstream phases only flip the flag + bind a class.
STAGE_REGISTRY: list[StageSpec] = [
    StageSpec("LOAD", enabled=True),
    StageSpec("PRE_GUARDRAIL", enabled=True),
    StageSpec("MEMORY_RETRIEVE", enabled=False),  # 📋 Phase 6
    StageSpec("RAG_QUERY", enabled=False),  # 📋 Phase 5
    StageSpec("SKILL_LOAD", enabled=False),  # 📋 Phase 8
    StageSpec("PROMPT_BUILD", enabled=True),
    StageSpec("LLM", enabled=True),
    StageSpec("TOOL_LOOP", enabled=False),  # 📋 Phase 7
    StageSpec("POST_GUARDRAIL", enabled=True),
    StageSpec("MEMORY_WRITE", enabled=False),  # 📋 Phase 6
]

#: The terminal stage that ALWAYS runs (finally-style), even on short-circuit.
EVENT_STAGE_NAME = "EVENT"


def apply_stage_flags(settings: Any) -> None:
    """Apply the env-driven ``STAGE_ENABLE_<NAME>`` flags to the registry (WP02).

    Consulted at startup (the api-layer lifespan) so future stages can be enabled per
    environment without code edits: each registry slot ``<NAME>`` reads the Settings
    field ``stage_enable_<name>`` (env var ``STAGE_ENABLE_<NAME>``). A slot without a
    matching Settings field keeps its in-code default — the registry stays authoritative
    for the pipeline SHAPE; the flags only flip ``enabled``.
    """
    for spec in STAGE_REGISTRY:
        value = getattr(settings, f"stage_enable_{spec.name.lower()}", None)
        if value is not None:
            spec.enabled = bool(value)


def bind_stage(name: str, stage_cls: type[Stage]) -> None:
    """Bind a concrete Stage subclass to its registry slot (called by the feature agent).

    Raises KeyError if ``name`` is not a known registry slot (and is not the EVENT
    stage, which is bound separately into the Pipeline). This keeps the pipeline shape
    authoritative here while letting feature agents supply the implementations.
    """
    for spec in STAGE_REGISTRY:
        if spec.name == name:
            spec.stage_cls = stage_cls
            return
    raise KeyError(f"Unknown stage registry slot: {name!r}")


# ── Pipeline runner ────────────────────────────────────────────────────────────
class Pipeline:
    """Executes enabled, bound stages in order; EVENT runs last as a finally-stage."""

    def __init__(self, stages: list[Stage], event_stage: Stage) -> None:
        self._stages = stages
        self._event_stage = event_stage

    @classmethod
    def from_registry(cls, event_stage: Stage) -> Pipeline:
        """Build a Pipeline from the bound + enabled entries in ``STAGE_REGISTRY``.

        Skips disabled slots and slots without a bound class (so a partially-bound
        registry still produces a runnable first-cycle pipeline). ``event_stage`` is
        supplied separately — it is the finally-equivalent terminal stage.
        """
        stages: list[Stage] = []
        for spec in STAGE_REGISTRY:
            if not spec.enabled or spec.stage_cls is None:
                continue
            stage = spec.stage_cls()
            stage.name = spec.name
            stage.enabled = spec.enabled
            stages.append(stage)
        return cls(stages, event_stage)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Run each enabled stage in order; short-circuit on terminal_error; EVENT last.

        Returns the (mutated) context. EVENT is ALWAYS executed — on the success path
        AND after a short-circuit — so the task row + Kafka event are always written.

        Cooperative cancellation (WP08): the cancel signal is polled BEFORE each stage
        and, for the LLM stage, WHILE it is in flight (the in-flight model round-trip is
        cancelled too). Observing the signal sets a ``cancelled`` terminal error and
        short-circuits to EVENT (which marks the task ``cancelled`` + emits the terminal
        event). Per-task timeout is applied by the api layer via ``asyncio.timeout``
        WRAPPING this call; a TimeoutError there short-circuits to EVENT the same way.
        """
        import time

        for stage in self._stages:
            if not stage.enabled:
                continue
            if ctx.terminal_error is not None:
                break
            # Poll the cancel signal BETWEEN stages (the cheap, always-on checkpoint).
            if await ctx.is_cancel_requested():
                self._mark_cancelled(ctx)
                break
            await self._run_one(stage, ctx, time)

        # EVENT is the finally-equivalent stage: always runs, even after a short-circuit
        # or an exception above. An EVENT failure is logged but never re-raised (the
        # outbox keeps the event durable; the api layer still returns a response).
        try:
            await self._event_stage.run(ctx)
        except Exception as exc:  # noqa: BLE001 — EVENT must never crash the response
            metrics.event_write_failed_total.labels("event_stage_error").inc()
            logger.error("event_stage_failed", error=str(exc), exc_info=exc)
        return ctx

    @staticmethod
    def _mark_cancelled(ctx: PipelineContext) -> None:
        """Record an observed cancel as the terminal state (idempotent — first wins)."""
        if ctx.terminal_error is None:
            logger.info("task_cancel_observed", task_id=ctx.task.task_id)
            ctx.fail(ErrorCode.INTERNAL_ERROR, "Task cancelled by request.", status="cancelled")

    async def _run_one(self, stage: Stage, ctx: PipelineContext, time: Any) -> None:
        started = time.monotonic()
        try:
            if stage.name == LLM_STAGE_NAME and ctx.cancel_check is not None:
                # Race the in-flight model round-trip against the cancel poll so a hung /
                # long LLM call is aborted promptly, not only observed AFTER it returns.
                await self._run_cancellable(stage, ctx)
            else:
                await stage.run(ctx)
        except _CancelledByRequest:
            self._mark_cancelled(ctx)
            outcome = "short_circuit"
        except Exception as exc:  # noqa: BLE001 — convert to a terminal error; EVENT still runs
            logger.error("stage_unhandled_exception", stage=stage.name, error=str(exc), exc_info=exc)
            ctx.fail("INTERNAL_ERROR", f"Stage {stage.name} failed: {exc}")
            outcome = "error"
        else:
            outcome = "short_circuit" if ctx.terminal_error is not None else "ok"
        metrics.stage_duration_seconds.labels(stage.name, outcome).observe(time.monotonic() - started)

    @staticmethod
    async def _run_cancellable(stage: Stage, ctx: PipelineContext) -> None:
        """Run ``stage`` while polling the cancel signal; cancel the stage on a hit.

        Raises :class:`_CancelledByRequest` (caught by ``_run_one``) when the signal
        fires — the stage task is cancelled, so an in-flight LLM HTTP request is torn
        down promptly. The stage coroutine running to completion first is the normal path.
        """
        stage_task: asyncio.Task[None] = asyncio.ensure_future(stage.run(ctx))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {stage_task}, timeout=_CANCEL_POLL_INTERVAL_SECONDS
                )
                if stage_task in done:
                    stage_task.result()  # re-raise any stage exception to _run_one
                    return
                if await ctx.is_cancel_requested():
                    raise _CancelledByRequest
        finally:
            if not stage_task.done():
                stage_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown only
                    await stage_task


class _CancelledByRequest(Exception):
    """Internal signal: the cancel flag fired while a (cancellable) stage was in flight."""
