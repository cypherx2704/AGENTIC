"""Unit tests for the sub-agent executor's pure pieces (phase B2b).

The run glue (:func:`run_subagent_task`) is integration-level (needs the pipeline + DB); here we
cover the deterministic units: the token mint/cache, the sub-agent principal, and the summary-only
result extraction.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.core.auth import Principal
from agent_runtime.core.config import get_settings
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.orchestration import executor
from agent_runtime.orchestration.executor import (
    SUB_AGENT,
    SubAgentResult,
    SubAgentTokenProvider,
    build_subagent_principal,
    result_from_context,
    run_subagent_task,
)
from agent_runtime.services.auth_client import MintedSubAgentToken

ORCH = "11111111-1111-4111-8111-111111111111"
SUB = "22222222-2222-4222-8222-222222222222"
TEN = "550e8400-e29b-41d4-a716-446655440000"


class _FakeAuth:
    """Minimal stand-in for AuthClient.mint_sub_agent_token — counts mints, echoes scopes."""

    def __init__(self, expires_in: int = 3600) -> None:
        self.calls = 0
        self.last_scopes: list[str] | None = None
        self._expires_in = expires_in

    async def mint_sub_agent_token(
        self, sub_agent_id: str, *, agent_jwt: str, requested_scopes: list[str] | None = None
    ) -> MintedSubAgentToken:
        self.calls += 1
        self.last_scopes = requested_scopes
        return MintedSubAgentToken(
            token=f"tok-{self.calls}", expires_in=self._expires_in, scopes=requested_scopes or []
        )


# ── token provider ───────────────────────────────────────────────────────────────────────
async def test_token_provider_caches_until_expiry() -> None:
    clock = [0.0]
    auth = _FakeAuth(expires_in=3600)
    provider = SubAgentTokenProvider(auth, safety_margin_seconds=60, now=lambda: clock[0])  # type: ignore[arg-type]

    t1 = await provider.get(SUB, orchestrator_jwt="orch")
    t2 = await provider.get(SUB, orchestrator_jwt="orch")
    assert t1 == t2 == "tok-1"
    assert auth.calls == 1  # second call served from cache

    clock[0] = 3600.0  # past expiry (0 + 3600 - 60 margin = 3540)
    t3 = await provider.get(SUB, orchestrator_jwt="orch")
    assert t3 == "tok-2"
    assert auth.calls == 2


async def test_token_provider_keys_by_scopes() -> None:
    auth = _FakeAuth()
    provider = SubAgentTokenProvider(auth, now=lambda: 0.0)  # type: ignore[arg-type]
    await provider.get(SUB, orchestrator_jwt="o", requested_scopes=["llm:invoke"])
    await provider.get(SUB, orchestrator_jwt="o", requested_scopes=["llm:invoke", "tool:invoke"])
    assert auth.calls == 2  # different scope sets are cached separately


async def test_token_provider_scope_order_is_stable_cache_key() -> None:
    auth = _FakeAuth()
    provider = SubAgentTokenProvider(auth, now=lambda: 0.0)  # type: ignore[arg-type]
    await provider.get(SUB, orchestrator_jwt="o", requested_scopes=["a", "b"])
    await provider.get(SUB, orchestrator_jwt="o", requested_scopes=["b", "a"])
    assert auth.calls == 1  # same set in a different order -> one cache entry


# ── principal ────────────────────────────────────────────────────────────────────────────
def test_build_subagent_principal() -> None:
    p = build_subagent_principal(
        sub_agent_id=SUB, tenant_id=TEN, scopes=["llm:invoke"], token="jwt-x", orchestrator_id=ORCH
    )
    assert p.agent_id == SUB
    assert p.tenant_id == TEN
    assert p.agent_type == SUB_AGENT
    assert p.parent_orchestrator_id == ORCH
    # raw_token is the sub-agent JWT (forwarded downstream); on_behalf_of derives from agent_id == SUB.
    assert p.raw_token == "jwt-x"
    assert p.raw_claims == {}


# ── result extraction (summary-only) ──────────────────────────────────────────────────────
def _ctx() -> PipelineContext:
    task = TaskRow(
        task_id="tid", agent_id=SUB, tenant_id=TEN, trace_id="tr", status="running", input={}
    )
    principal = build_subagent_principal(
        sub_agent_id=SUB, tenant_id=TEN, scopes=[], token="t", orchestrator_id=ORCH
    )
    return PipelineContext(
        principal=principal, inbound_agent_jwt="t", trace_id="tr", request_id="rq", task=task
    )


def test_result_success() -> None:
    ctx = _ctx()
    ctx.final_answer = "the synthesized answer"
    ctx.tokens_used = 42
    ctx.cost_usd = 0.01
    r = result_from_context(ctx)
    assert r.status == "completed" and r.is_success
    assert r.summary == "the synthesized answer"
    assert r.tokens_used == 42 and r.cost_usd == 0.01
    assert r.error_code is None
    assert r.to_output() == {"summary": "the synthesized answer", "citations": []}


def test_result_failure_carries_error() -> None:
    ctx = _ctx()
    ctx.fail("BUDGET_EXCEEDED", "over budget", status="failed")
    r = result_from_context(ctx)
    assert not r.is_success
    assert r.status == "failed"
    assert r.error_code == "BUDGET_EXCEEDED"
    assert r.error_msg == "over budget"


def test_result_dedupes_citations() -> None:
    ctx = _ctx()
    ctx.final_answer = "answer"
    ctx.rag_chunks = [
        {"document_id": "d1"},
        {"document_id": "d1"},  # duplicate
        {"document_id": "d2"},
        {"score": 0.5},  # no document_id
    ]
    r = result_from_context(ctx)
    assert r.citations == ["d1", "d2"]


def test_result_to_output_is_summary_only() -> None:
    r = SubAgentResult(task_id="t", status="completed", summary="s", citations=["c1"])
    # Only the summary + citations cross back to the orchestrator — never the transcript/steps.
    assert set(r.to_output().keys()) == {"summary", "citations"}


# ── run_subagent_task: the TWO identity branches ─────────────────────────────────────────
# Regression: when the planner assigns a node to the ORCHESTRATOR ITSELF ("no delegation needed"),
# the mint is skipped — but `token` is also the pipeline's inbound_agent_jwt, so leaving it bound
# only on the delegated branch raised UnboundLocalError and crashed the node. Both branches must
# bind it. This is the seam the pure-unit tests deliberately skipped, so it goes untested no more.
TASK = "33333333-3333-4333-8333-333333333333"


def _orchestrator() -> Principal:
    return Principal(
        tenant_id=TEN, agent_id=ORCH, scopes=["agent:execute", "llm:invoke"],
        principal_type="agent", raw_token="ORCH-JWT", raw_claims={},
    )


def _install_run_stubs(monkeypatch: Any, captured: dict[str, Any]) -> None:
    """Stub the DB + pipeline so run_subagent_task's IDENTITY logic can be exercised alone."""

    async def _create_task(pool: Any, **kw: Any) -> TaskRow:
        captured["created_agent_id"] = kw["agent_id"]
        return TaskRow(
            task_id=TASK, agent_id=kw["agent_id"], tenant_id=kw["tenant_id"],
            trace_id=kw["trace_id"], status="pending", input=kw["task_input"],
        )

    async def _mark_running(pool: Any, tenant_id: str, task_id: str) -> None:
        return None

    async def _run_pipeline(ctx: PipelineContext, settings: Any, budget: float) -> None:
        captured["ctx"] = ctx
        ctx.final_answer = "done"

    monkeypatch.setattr(executor.tasks_repo, "create_task", _create_task)
    monkeypatch.setattr(executor.tasks_repo, "mark_running", _mark_running)
    monkeypatch.setattr(executor, "_run_pipeline_guarded", _run_pipeline)


async def test_self_run_skips_the_mint_and_forwards_the_orchestrator_jwt(monkeypatch: Any) -> None:
    """Node assigned to the orchestrator: no sub-agent token is minted (Auth would 404 — the
    orchestrator is not its own sub-agent), and its OWN verified JWT is forwarded downstream."""
    captured: dict[str, Any] = {}
    _install_run_stubs(monkeypatch, captured)
    auth = _FakeAuth()
    orch = _orchestrator()

    result = await run_subagent_task(
        pool=object(), settings=get_settings(), token_provider=SubAgentTokenProvider(auth),  # type: ignore[arg-type]
        orchestrator=orch, sub_agent_id=ORCH,  # <-- the orchestrator ITSELF
        workflow_id="w", parent_task_id=None, message="what is 17 * 23?",
        trace_id="t", request_id="r", budget_seconds=30.0,
    )

    assert auth.calls == 0                                   # never asked Auth to mint
    ctx = captured["ctx"]
    assert ctx.principal is orch                             # ran under the orchestrator principal
    assert ctx.inbound_agent_jwt == "ORCH-JWT"               # <-- the UnboundLocalError line
    assert captured["created_agent_id"] == ORCH
    assert result.status == "completed"


async def test_delegated_run_mints_and_forwards_the_subagent_jwt(monkeypatch: Any) -> None:
    """Node assigned to a real sub-agent: mint its identity and forward THAT jwt (downstream
    confinement keys off the forwarded token's agent_id)."""
    captured: dict[str, Any] = {}
    _install_run_stubs(monkeypatch, captured)
    auth = _FakeAuth()
    orch = _orchestrator()

    result = await run_subagent_task(
        pool=object(), settings=get_settings(), token_provider=SubAgentTokenProvider(auth),  # type: ignore[arg-type]
        orchestrator=orch, sub_agent_id=SUB,  # <-- a real sub-agent
        workflow_id="w", parent_task_id=None, message="research it",
        trace_id="t", request_id="r", budget_seconds=30.0,
        requested_scopes=["agent:execute"],
    )

    assert auth.calls == 1                                   # minted exactly once
    ctx = captured["ctx"]
    assert ctx.principal.agent_id == SUB                     # ran AS the sub-agent
    assert ctx.inbound_agent_jwt == "tok-1"                  # the MINTED token, not the orchestrator's
    assert captured["created_agent_id"] == SUB
    assert result.status == "completed"


# ── the cache MUST NOT outlive the token (401 "Signature has expired" regression) ────────
def _jwt_with_exp(exp_epoch: float) -> str:
    """A syntactically valid unsigned JWT carrying only an `exp` claim."""
    import base64 as _b64
    import json as _json

    def seg(d: dict[str, Any]) -> str:
        raw = _json.dumps(d).encode()
        return _b64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg({'exp': int(exp_epoch)})}.sig"


def test_jwt_exp_reads_the_claim() -> None:
    from agent_runtime.orchestration.executor import jwt_exp

    assert jwt_exp(_jwt_with_exp(1_700_000_000)) == 1_700_000_000.0
    assert jwt_exp("not-a-jwt") is None
    assert jwt_exp("a.b.c") is None  # unparseable payload


class _AuthWithExp:
    """Mints a token whose REPORTED expires_in disagrees with its signed `exp`."""

    def __init__(self, *, reported: int, real_lifetime: float, wall: float) -> None:
        self.calls = 0
        self._reported = reported
        self._real = real_lifetime
        self._wall = wall

    async def mint_sub_agent_token(
        self, sub_agent_id: str, *, agent_jwt: str, requested_scopes: list[str] | None = None
    ) -> MintedSubAgentToken:
        self.calls += 1
        return MintedSubAgentToken(
            token=_jwt_with_exp(self._wall + self._real),
            expires_in=self._reported,
            scopes=requested_scopes or [],
        )


async def test_cache_honours_the_jwt_exp_not_the_reported_ttl() -> None:
    """THE BUG: Auth reported a long expires_in while signing a 1h token. The cache trusted the
    number, served the token for 2.5h, and every downstream then rejected it with
    `401 Invalid token: Signature has expired`. The cache must expire on the EARLIER of the two.
    """
    wall = 1_700_000_000.0
    mono = [0.0]
    # Auth *claims* 9999s, but the JWT it signed actually lives 3600s.
    auth = _AuthWithExp(reported=9999, real_lifetime=3600.0, wall=wall)
    provider = SubAgentTokenProvider(
        auth,  # type: ignore[arg-type]
        now=lambda: mono[0],
        wall_now=lambda: wall,
    )

    await provider.get(SUB, orchestrator_jwt="O")
    assert auth.calls == 1

    # 1h59m later: the reported TTL (9999s) says "still fresh" — but the JWT died at 3600s.
    mono[0] = 7140.0
    await provider.get(SUB, orchestrator_jwt="O")
    assert auth.calls == 2, "must RE-MINT: the signed token is long dead despite expires_in"


async def test_cache_still_caches_within_the_real_lifetime() -> None:
    """The fix must not destroy the cache — a genuinely-fresh token is still reused."""
    wall = 1_700_000_000.0
    mono = [0.0]
    auth = _AuthWithExp(reported=3600, real_lifetime=3600.0, wall=wall)
    provider = SubAgentTokenProvider(
        auth,  # type: ignore[arg-type]
        now=lambda: mono[0],
        wall_now=lambda: wall,
    )

    await provider.get(SUB, orchestrator_jwt="O")
    mono[0] = 60.0  # a minute later — well inside the hour
    await provider.get(SUB, orchestrator_jwt="O")
    assert auth.calls == 1  # served from cache, no needless re-mint


async def test_missing_expires_in_falls_back_to_the_exp_claim() -> None:
    """expires_in absent (parsed as 0) must NOT collapse to a 1s cache when the JWT states its exp."""
    wall = 1_700_000_000.0
    mono = [0.0]
    auth = _AuthWithExp(reported=0, real_lifetime=3600.0, wall=wall)
    provider = SubAgentTokenProvider(
        auth,  # type: ignore[arg-type]
        now=lambda: mono[0],
        wall_now=lambda: wall,
    )

    await provider.get(SUB, orchestrator_jwt="O")
    mono[0] = 600.0  # 10 min in — the exp claim says there is plenty left
    await provider.get(SUB, orchestrator_jwt="O")
    assert auth.calls == 1
