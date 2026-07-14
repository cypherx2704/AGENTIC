"""Sub-agent executor (phase B2b) — run ONE sub-agent's pipeline under its OWN identity.

The orchestration driver (B2c) calls :func:`run_subagent_task` for each DAG node. The sub-agent
runs the EXISTING single-agent pipeline (LOAD -> ... -> EVENT), but under a freshly-minted
**sub-agent JWT** so downstream confinement (LLMs alias allowlist, Tools access) is enforced
against the SUB-AGENT's ``agent_id`` — the confinement key is the ``agent_id`` of the JWT in
``X-Forwarded-Agent-JWT`` (== the service token's ``on_behalf_of``), never the task's agent_id.
See the identity investigation in ``SUBAGENT_WORKFLOW_PLAN.md``.

No A2A: the token is minted via the Auth *delegation* endpoint (``POST
/v1/orchestrator/sub-agents/{id}/token``, authenticated by the orchestrator's own JWT), in-tenant.
Only a SUMMARIZED result (``{summary, citations}``) is returned to the orchestrator — never the
sub-agent's transcript (the biggest token lever, per plan §7 #1).

Testable pieces (token cache, principal construction, result extraction) are pure; the run glue is
integration-level (needs the pipeline + DB) and is exercised by the driver + the review pass.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import stages
from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ErrorCode
from ..core.pipeline import Pipeline, PipelineContext
from ..db import tasks_repo
from ..db.steps_repo import StepBuffer
from ..services.auth_client import AuthClient

logger = structlog.get_logger(__name__)

SUB_AGENT = "sub_agent"


def jwt_exp(token: str) -> float | None:
    """Read a JWT's ``exp`` claim WITHOUT verifying the signature.

    We are not trusting the token here — Auth just issued it to us over TLS, and every downstream
    re-verifies the signature itself. We only need the DEADLINE, and ``exp`` is the single value
    every verifier actually enforces. Returns ``None`` if the token is unparseable.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — an unreadable token just means "no deadline known"
        return None


# ── sub-agent token provider (mint + expiry-aware cache) ─────────────────────────────────
class SubAgentTokenProvider:
    """Mints + caches scoped sub-agent JWTs via the Auth delegation endpoint.

    A run touches each sub-agent a few times; caching (keyed by ``(sub_agent_id, scopes)`` with a
    safety margin before the token's expiry) avoids re-minting per node. ``now`` is injectable for
    tests (defaults to ``time.monotonic``).

    **The cache MUST NOT outlive the token.** It therefore expires on the EARLIER of Auth's reported
    ``expires_in`` and the token's own ``exp`` claim. Trusting the reported number alone is what
    broke: a token whose ``exp`` was one hour out stayed cached for two and a half, and every
    downstream then rejected it with ``401 Invalid token: Signature has expired``. ``exp`` is what
    verifiers enforce, so ``exp`` is what the cache honours.
    """

    def __init__(
        self,
        auth_client: AuthClient,
        *,
        safety_margin_seconds: float = 60.0,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], float] = time.time,
    ) -> None:
        self._auth = auth_client
        self._margin = safety_margin_seconds
        self._now = now
        self._wall_now = wall_now  # `exp` is a wall-clock epoch; `now` is monotonic
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[str, float]] = {}

    def _lifetime_seconds(self, token: str, reported_expires_in: int) -> float:
        """Seconds this token is ACTUALLY usable for: the earlier of what Auth said and what it signed."""
        candidates: list[float] = []
        if reported_expires_in > 0:
            candidates.append(float(reported_expires_in))
        exp = jwt_exp(token)
        if exp is not None:
            candidates.append(exp - self._wall_now())
        if not candidates:
            return 1.0  # nothing to go on -> re-mint next time rather than cache blind
        lifetime = min(candidates)
        if len(candidates) == 2 and abs(candidates[0] - candidates[1]) > 60.0:
            logger.warning(
                "subagent_token_ttl_mismatch",
                reported_expires_in=reported_expires_in,
                exp_remaining=round(candidates[1]),
                using=round(lifetime),
                hint="Auth's expires_in disagrees with the JWT exp; honouring exp.",
            )
        return lifetime

    async def get(
        self,
        sub_agent_id: str,
        *,
        orchestrator_jwt: str,
        requested_scopes: list[str] | None = None,
    ) -> str:
        """Return a valid sub-agent JWT (cached if still comfortably unexpired, else minted)."""
        scopes = tuple(sorted(requested_scopes or []))
        key = (sub_agent_id, scopes)
        now = self._now()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        minted = await self._auth.mint_sub_agent_token(
            sub_agent_id, agent_jwt=orchestrator_jwt, requested_scopes=list(scopes)
        )
        lifetime = self._lifetime_seconds(minted.token, minted.expires_in)
        expiry = now + max(1.0, lifetime - self._margin)
        self._cache[key] = (minted.token, expiry)
        return minted.token


# ── sub-agent principal ──────────────────────────────────────────────────────────────────
def build_subagent_principal(
    *,
    sub_agent_id: str,
    tenant_id: str,
    scopes: list[str],
    token: str,
    orchestrator_id: str,
) -> Principal:
    """Build the :class:`Principal` a sub-agent pipeline runs under.

    ``raw_token`` = the minted sub-agent JWT — it is forwarded verbatim as ``X-Forwarded-Agent-JWT``
    downstream, and the stages derive ``on_behalf_of = principal.agent_id`` (= the sub-agent). Both
    therefore resolve to the sub-agent, which is exactly what the downstream confinement keys off.
    ``raw_claims`` is empty (downstream re-verifies the JWT itself; ax-1 only forwards it).
    """
    return Principal(
        tenant_id=tenant_id,
        agent_id=sub_agent_id,
        scopes=list(scopes),
        principal_type="agent",
        api_key_id=None,
        raw_token=token,
        raw_claims={},
        kid=None,
        agent_type=SUB_AGENT,
        parent_orchestrator_id=orchestrator_id,
    )


# ── result (summary-only) ────────────────────────────────────────────────────────────────
@dataclass
class SubAgentResult:
    """The summarized outcome of a sub-agent node — what flows back to the orchestrator."""

    task_id: str
    status: str  # completed | failed | timeout | cancelled
    summary: str | None
    citations: list[str] = field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0
    error_code: str | None = None
    error_msg: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == "completed"

    def to_output(self) -> dict[str, Any]:
        """The JSON stored on ``workflow_tasks.output`` (summary + citations — NOT the transcript)."""
        return {"summary": self.summary, "citations": self.citations}


def result_from_context(ctx: PipelineContext) -> SubAgentResult:
    """Extract the summary-only :class:`SubAgentResult` from a finished pipeline context."""
    err = ctx.terminal_error
    status = err.status if err is not None else "completed"
    citations: list[str] = []
    for chunk in ctx.rag_chunks:
        doc = chunk.get("document_id")
        if isinstance(doc, str) and doc and doc not in citations:
            citations.append(doc)
    return SubAgentResult(
        task_id=ctx.task.task_id,
        status=status,
        summary=ctx.final_answer,
        citations=citations,
        tokens_used=ctx.tokens_used,
        cost_usd=ctx.cost_usd,
        error_code=(err.code if err is not None else None),
        error_msg=(err.message if err is not None else None),
    )


# ── run glue (integration) ───────────────────────────────────────────────────────────────
async def _noop_publish(*_args: Any, **_kwargs: Any) -> None:
    """No-op SSE publisher — the DRIVER owns run-level streaming; node runs don't self-publish."""
    return None


async def _run_pipeline_guarded(
    ctx: PipelineContext, settings: Settings, budget: float
) -> None:
    """Run the pipeline under the per-node timeout, finalising via EVENT on overrun.

    Mirrors ``api.tasks._run_pipeline_guarded`` (kept local so the executor does not depend on the
    api layer and cannot form an import cycle with the future run endpoint). EVENT always runs inside
    ``Pipeline.run``; on a budget TimeoutError the stages were cancelled before EVENT, so we mark the
    task ``timeout`` and run a fresh EVENT to finalise the row + emit the terminal event.
    """
    event_stage = stages.EventStage(producer_version=settings.service_version)
    pipeline = Pipeline.from_registry(event_stage)
    try:
        async with asyncio.timeout(budget):
            await pipeline.run(ctx)
    except TimeoutError:
        logger.warning("subagent_task_timed_out", task_id=ctx.task.task_id, budget_s=budget)
        if ctx.terminal_error is None:
            ctx.fail(ErrorCode.SERVICE_UNAVAILABLE, "Sub-agent task exceeded its budget.", status="timeout")
        # Fail-soft finalise (mirrors api.tasks._finalise_after_timeout): an EVENT/outbox write error
        # during timeout finalisation must not raise out of the executor (the sweeper backstops it).
        try:
            await stages.EventStage(producer_version=settings.service_version).run(ctx)
        except Exception as exc:  # noqa: BLE001 — timeout finalisation is best-effort
            logger.error("subagent_timeout_finalise_failed", task_id=ctx.task.task_id, error=str(exc))


async def run_subagent_task(
    *,
    pool: AsyncConnectionPool,
    settings: Settings,
    token_provider: SubAgentTokenProvider,
    orchestrator: Principal,
    sub_agent_id: str,
    workflow_id: str,
    parent_task_id: str | None,
    message: str,
    trace_id: str,
    request_id: str,
    budget_seconds: float,
    cost_budget_usd: float | None = None,
    use_tools: bool = True,
    requested_scopes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    cancel_check: Callable[[], Any] | None = None,
    on_task_created: Callable[[str], Awaitable[None]] | None = None,
) -> SubAgentResult:
    """Run one sub-agent node: mint its identity, spawn a lineage-linked task, run the pipeline.

    The child task is a real ``xagent.tasks`` row (``agent_id = sub_agent``, ``parent_task_id`` +
    ``workflow_id`` set) so it reuses ax-1's full reliability envelope (timeout, cancel, sweeper
    crash-recovery). Returns a SUMMARY-ONLY :class:`SubAgentResult` — the orchestrator never ingests
    the sub-agent's transcript. Authorization (orchestrator owns this sub-agent) is enforced upstream
    by the Auth mint endpoint (404 for a non-owned target) and by the driver's DAG-assignment guard.

    ``on_task_created`` is invoked with the child ``task_id`` the moment the row exists — BEFORE the
    pipeline runs. The driver uses it to stamp ``workflow_tasks.task_id`` immediately, so the live
    execution tree can follow the sub-agent's audit trail (its tool calls) WHILE it works. Stamping
    the id only on completion, as it used to, meant tools were invisible for the entire time the node
    was actually doing them — you could only see them afterwards, if at all.
    """
    # A node may be assigned to the ORCHESTRATOR ITSELF ("no delegation needed" — the planner's
    # default outcome). Delegation-minting is only for real sub-agents: asking Auth to mint a
    # sub-agent token for the orchestrator would 404 (it is not its own sub-agent). Run it under
    # the orchestrator's own already-verified principal instead.
    # `token` is ALSO the pipeline's `inbound_agent_jwt` below (forwarded downstream as
    # X-Forwarded-Agent-JWT) — so BOTH branches must bind it, or the self-run path raises
    # UnboundLocalError. Self-run forwards the orchestrator's own already-verified JWT, which is
    # exactly what downstream confinement should key off when the orchestrator is the actor.
    if sub_agent_id == orchestrator.agent_id:
        token = orchestrator.raw_token or ""
        principal = orchestrator
    else:
        token = await token_provider.get(
            sub_agent_id, orchestrator_jwt=orchestrator.raw_token, requested_scopes=requested_scopes
        )
        principal = build_subagent_principal(
            sub_agent_id=sub_agent_id,
            tenant_id=orchestrator.tenant_id,
            scopes=requested_scopes or [],
            token=token,
            orchestrator_id=orchestrator.agent_id or "",
        )

    task = await tasks_repo.create_task(
        pool,
        tenant_id=orchestrator.tenant_id,
        agent_id=sub_agent_id,
        trace_id=trace_id,
        task_input={"message": message},
        timeout_seconds=max(1, int(budget_seconds)),
        metadata=metadata or {},
        cost_budget_per_task=cost_budget_usd,
        parent_task_id=parent_task_id,
        workflow_id=workflow_id,
    )
    if on_task_created is not None:
        # Publish the child task id NOW (not at completion) so the run's execution tree can stream
        # this sub-agent's steps while they happen. Fail-soft: a bookkeeping hiccup must never stop
        # the sub-agent from actually running.
        try:
            await on_task_created(task.task_id)
        except Exception as exc:  # noqa: BLE001 — telemetry, not control flow
            logger.warning("subagent_task_id_publish_failed", task_id=task.task_id, error=str(exc))
    await tasks_repo.mark_running(pool, orchestrator.tenant_id, task.task_id)

    ctx = PipelineContext(
        principal=principal,
        inbound_agent_jwt=token,
        trace_id=trace_id,
        request_id=request_id,
        task=task,
        steps=StepBuffer(),
        pool=pool,
        started_monotonic=time.monotonic(),
        started_at=tasks_repo.now_iso(),
        cancel_check=cancel_check,
        cost_budget_usd=cost_budget_usd,
        # The single chokepoint for the run-level tool switch: every sub-agent node of every run
        # (solo and subagents alike) is built here, so TOOL_LOOP sees the caller's choice.
        use_tools=use_tools,
    )
    ctx.publish_event = _noop_publish  # type: ignore[attr-defined]

    await _run_pipeline_guarded(ctx, settings, budget_seconds)
    return result_from_context(ctx)
