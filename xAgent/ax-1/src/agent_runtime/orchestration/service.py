"""Orchestration coordinator (phase B5) — assembles decompose + driver + LLM + HIL + cancel.

The run endpoint (``api/orchestrations.py``) creates a run and kicks off :meth:`drive` as a background
job. This module is the ONLY place that wires the concrete clients (llms-gateway, HIL, Valkey cancel,
Auth delegation-mint) into the otherwise-decoupled driver — so the driver + executor stay unit-testable
and this glue is verified by import + the end-to-end drive.

Never leaves a workflow non-terminal: any error before/inside the driver finalises the row ``failed``.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db import agents_repo
from ..services.auth_client import AuthClient
from ..services.hil_client import HilClient, HilVerdict
from ..services.llms_client import LlmsClient
from ..services.valkey import ValkeyClient
from . import authz, repo
from .decompose import RetryApprover, decompose
from .driver import WorkflowOutcome, run_workflow
from .executor import SubAgentTokenProvider
from .llm import (
    ORCHESTRATOR_TARGET,
    AgentCapability,
    LlmResult,
    make_llm_planner,
    make_orchestrator_complete,
)
from .llm import synthesize as llm_synthesize

logger = structlog.get_logger(__name__)

#: TTL for a workflow cancel flag (long enough to outlast any run; self-evicts).
_CANCEL_TTL_SECONDS = 3600
#: Fallback orchestrator model when the orchestrator has no registered runtime row.
_DEFAULT_MODEL = "smart"
#: The HIL operation the plan-repair gate asks about (Auth keys its policy off this string).
_PLAN_REPAIR_OPERATION = "orchestration_plan_repair"


class OrchestrationCoordinator:
    """Creates + drives orchestration runs. One instance per app (holds a token cache)."""

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        settings: Settings,
        valkey: ValkeyClient,
        llms_client: LlmsClient,
        auth_client: AuthClient,
        hil_client: HilClient | None = None,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._valkey = valkey
        self._llms = llms_client
        self._hil = hil_client
        self._token_provider = SubAgentTokenProvider(auth_client)

    @staticmethod
    def _cancel_key(workflow_id: str) -> str:
        return f"wf:{workflow_id}"

    async def create_run(
        self,
        orchestrator: Principal,
        *,
        goal: str,
        mode: str = "subagents",
        cost_budget_usd: float | None = None,
        timeout_seconds: int | None = None,
    ) -> repo.WorkflowRow:
        """Create a ``pending`` workflow row (orchestrator-only). Caller then schedules :meth:`drive`."""
        authz.require_orchestrator(orchestrator)
        return await repo.create_workflow(
            self._pool,
            tenant_id=orchestrator.tenant_id,
            root_agent_id=orchestrator.agent_id or "",
            goal=goal.strip(),
            mode=mode,
            cost_budget_usd=cost_budget_usd,
            timeout_seconds=timeout_seconds,
        )

    async def request_cancel(self, tenant_id: str, workflow_id: str) -> None:
        """Set the workflow cancel flag (raises on a Valkey error so the endpoint can 503)."""
        await self._valkey.set_cancel_signal(
            prefix=self._settings.task_signal_key_prefix,
            tenant_id=tenant_id,
            task_id=self._cancel_key(workflow_id),
            ttl_seconds=_CANCEL_TTL_SECONDS,
            timeout_seconds=self._settings.task_signal_valkey_timeout_seconds,
        )

    async def drive(
        self, orchestrator: Principal, workflow: repo.WorkflowRow, *, trace_id: str, request_id: str
    ) -> WorkflowOutcome:
        """Background body: build roster + DAG, then run the driver. Always finalises the workflow."""
        tid = orchestrator.tenant_id
        wid = workflow.workflow_id
        # Hoisted ABOVE the try: planning calls cost real money, and a run that dies BECAUSE the
        # planner failed still spent it. The failure path below reads these, so a failed
        # orchestration records what it burned instead of reporting a cost of nothing.
        plan_tokens, plan_cost = [0], [0.0]
        try:
            orchestrator_id = orchestrator.agent_id or ""
            refs = await repo.list_orchestrator_subagents(self._pool, tid, orchestrator_id)
            # NOTE: an EMPTY roster is not an error. An orchestrator with no sub-agents can still
            # answer its goal itself, and the planner is allowed to decide exactly that. The old
            # gate here failed such a run outright with UNASSIGNED_NODE — the backend insisting that
            # sub-agents exist before it would let the model decide it did not need any.
            roster = {r.name: r.agent_id for r in refs}
            # The orchestrator is itself a valid target: the planner picks it when the goal needs no
            # specialist ("no delegation"). It is ALSO the default for a preset-less node (the solo
            # graph) — never an arbitrary sub-agent.
            roster[ORCHESTRATOR_TARGET] = orchestrator_id
            default_agent_id = orchestrator_id
            # The planner routes on BOTH signals: `purpose` (the sub-agent's routing description —
            # what it is FOR) and `tools` (what it can physically DO). Description alone lets a
            # mis-described agent take work it has no tool for and fabricate an answer; tools alone
            # cannot tell two toolless agents apart. `SubAgentRef.purpose` falls back to the system
            # prompt for agents registered before the description column existed.
            capabilities = [
                AgentCapability(name=r.name, purpose=r.purpose, tools=tuple(r.allowed_tools))
                for r in refs
            ]

            runtime = await agents_repo.get_agent(self._pool, tid, orchestrator_id)
            model = runtime.llm_model if runtime is not None else _DEFAULT_MODEL
            complete = make_orchestrator_complete(self._llms, orchestrator=orchestrator, model=model)

            # Capture the decomposition planner's LLM spend so it's accrued to the run budget/total
            # (both the first plan and any repair attempt).
            async def plan_complete(messages: list[dict[str, Any]]) -> LlmResult:
                r = await complete(messages)
                plan_tokens[0] += r.tokens_used
                plan_cost[0] += r.cost_usd
                return r

            await self._transition(tid, workflow, "planning")
            # The PLANNER decides everything: which steps exist, which agent runs each one, and what
            # runs in parallel. We pass it the real roster and then only VALIDATE what it returns
            # (`targets` = every name it may legally use). An invalid plan is handed back to the
            # planner once — never silently rewritten here.
            decomp = await decompose(
                workflow.goal, workflow_id=wid, tenant_id=tid, mode=workflow.mode,
                planner=make_llm_planner(plan_complete, roster=capabilities),
                targets=sorted(roster),
                approve_retry=self._retry_approver(orchestrator, workflow),
            )
            updated = await repo.update_workflow(
                self._pool, tid, wid, expected_version=workflow.version,
                subtask_dag=decomp.dag_doc, decomposition=decomp.decomposition,
            )
            wf = updated if updated is not None else workflow
            wf.subtask_dag = decomp.dag_doc  # ensure the driver sees the DAG even if the persist raced

            async def synthesizer(goal: str, summaries: dict[str, str]) -> tuple[str, int, float]:
                r = await llm_synthesize(goal, summaries, complete=complete)
                return r.content, r.tokens_used, r.cost_usd

            return await run_workflow(
                pool=self._pool, settings=self._settings, token_provider=self._token_provider,
                orchestrator=orchestrator, workflow=wf, roster=roster, trace_id=trace_id,
                request_id=request_id, node_budget_seconds=float(self._settings.task_timeout_seconds),
                cost_budget_usd=workflow.cost_budget_usd, cancel_check=self._cancel_check(tid, wid),
                hil_gate=self._hil_gate(orchestrator), synthesizer=synthesizer,
                default_agent_id=default_agent_id,
                initial_tokens=plan_tokens[0], initial_cost=plan_cost[0],
            )
        except asyncio.CancelledError:
            # Process shutdown cancelled the drive mid-run — finalise the row (the lifespan drains
            # these tasks BEFORE closing the pool) so a deploy can't strand it, then honour cancellation.
            with contextlib.suppress(Exception):
                await self._fail(
                    tid, wid, None, ErrorCode.SERVICE_UNAVAILABLE, "Run interrupted by shutdown."
                )
            raise
        except ApiError as exc:
            # A structured orchestration failure (ORCHESTRATION_FAILED from the planner/repair loop,
            # INVALID_DAG, ...). Surface its OWN code + message on the workflow row — flattening it
            # to INTERNAL_ERROR would hide from the user that the *plan* was the problem, and what
            # about it was wrong.
            logger.warning(
                "orchestration_drive_failed", workflow_id=wid, code=exc.code, error=exc.message
            )
            return await self._fail(
                tid, wid, None, exc.code, exc.message,
                tokens_used=plan_tokens[0], cost_usd=plan_cost[0],
            )
        except Exception as exc:  # noqa: BLE001 — a run must ALWAYS finalise, never hang non-terminal
            logger.error("orchestration_drive_failed", workflow_id=wid, error=str(exc), exc_info=exc)
            return await self._fail(
                tid, wid, None, ErrorCode.INTERNAL_ERROR, f"Orchestration error: {exc}",
                tokens_used=plan_tokens[0], cost_usd=plan_cost[0],
            )

    # ── wiring helpers ─────────────────────────────────────────────────────────────────────
    async def _transition(self, tenant_id: str, wf: repo.WorkflowRow, status: str) -> None:
        """Move the workflow row to ``status``, keeping ``wf.version`` in sync (best-effort).

        Every later write is optimistically locked on ``wf.version``, so a transition that LOSES the
        race (something else bumped the row — a cancel, say) must re-read rather than leave a stale
        version behind: with a stale version every subsequent update silently no-ops and the run is
        stranded non-terminal.
        """
        updated = await repo.update_workflow(
            self._pool, tenant_id, wf.workflow_id, expected_version=wf.version, status=status
        )
        if updated is not None:
            wf.version, wf.status = updated.version, updated.status
            return
        current = await repo.get_workflow(self._pool, tenant_id, wf.workflow_id)
        if current is not None:
            wf.version, wf.status = current.version, current.status

    def _retry_approver(self, orchestrator: Principal, wf: repo.WorkflowRow) -> RetryApprover:
        """The plan-repair gate: tell the human the plan was rejected, and ask before re-planning.

        Returns True to let the planner try again, False to hard-fail the run. Mapping the HIL
        verdict is the entire reason :class:`HilVerdict` is tri-state rather than a bool:

        * ``DENIED``      -> False. A human read the reason and said no. The run fails.
        * ``GRANTED``     -> True.  Re-plan.
        * ``UNAVAILABLE`` -> True.  HIL is disabled, Auth is down, the request expired, or nobody
          answered inside the wait budget. Nobody REFUSED — so the retry proceeds, rather than a run
          dying because there was no one around to ask.

        The workflow reads ``awaiting_approval`` for the duration of the wait (so the UI can show why
        the run is paused) and returns to ``planning`` whatever the verdict.
        """
        tenant_id = orchestrator.tenant_id

        async def approve(reason: str) -> bool:
            await self._transition(tenant_id, wf, "awaiting_approval")
            try:
                if self._hil is None:
                    return True  # UNAVAILABLE — no HIL is wired at all; not a refusal.
                ctx = SimpleNamespace(inbound_agent_jwt=orchestrator.raw_token)
                verdict = await self._hil.request_verdict(
                    ctx,
                    operation_type=_PLAN_REPAIR_OPERATION,
                    context={
                        "workflow_id": wf.workflow_id,
                        "goal": wf.goal,
                        "reason": reason,
                        "action": "Let the orchestrator re-plan this goal and try again?",
                    },
                )
                return verdict is not HilVerdict.DENIED
            finally:
                await self._transition(tenant_id, wf, "planning")

        return approve

    def _cancel_check(self, tenant_id: str, workflow_id: str) -> Any:
        async def check() -> bool:
            return await self._valkey.is_cancelled(
                prefix=self._settings.task_signal_key_prefix, tenant_id=tenant_id,
                task_id=self._cancel_key(workflow_id),
                timeout_seconds=self._settings.task_signal_valkey_timeout_seconds,
            )

        return check

    def _hil_gate(self, orchestrator: Principal) -> Any:
        if self._hil is None:
            return None
        hil = self._hil
        ctx = SimpleNamespace(inbound_agent_jwt=orchestrator.raw_token)

        async def gate(operation_type: str, context: dict[str, Any]) -> bool:
            return await hil.request_and_wait(ctx, operation_type=operation_type, context=context)

        return gate

    async def _fail(
        self, tenant_id: str, workflow_id: str, expected_version: int | None, code: str, msg: str,
        *, tokens_used: int = 0, cost_usd: float = 0.0,
    ) -> WorkflowOutcome:
        """Finalise a workflow ``failed`` (re-reading the current version when unknown).

        ``tokens_used`` / ``cost_usd`` carry any spend the run had ALREADY incurred before it died —
        chiefly the planning calls. A run that fails because the planner could not produce a usable
        plan still burned those tokens; recording 0 would under-report real cost.
        """
        version = expected_version
        if version is None:
            wf = await repo.get_workflow(self._pool, tenant_id, workflow_id)
            if wf is None or wf.status in ("completed", "failed", "cancelled", "timeout"):
                return WorkflowOutcome(status="failed", output={}, error_code=code, error_msg=msg)
            version = wf.version
        await repo.update_workflow(
            self._pool, tenant_id, workflow_id, expected_version=version, status="failed",
            error_code=code, error_msg=msg, tokens_used=tokens_used, cost_usd=cost_usd,
            mark_completed=True,
        )
        return WorkflowOutcome(
            status="failed", output={}, error_code=code, error_msg=msg,
            tokens_used=tokens_used, cost_usd=cost_usd,
        )
