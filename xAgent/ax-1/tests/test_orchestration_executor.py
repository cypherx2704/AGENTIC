"""Unit tests for the sub-agent executor's pure pieces (phase B2b).

The run glue (:func:`run_subagent_task`) is integration-level (needs the pipeline + DB); here we
cover the deterministic units: the token mint/cache, the sub-agent principal, and the summary-only
result extraction.
"""

from __future__ import annotations

from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.orchestration.executor import (
    SUB_AGENT,
    SubAgentResult,
    SubAgentTokenProvider,
    build_subagent_principal,
    result_from_context,
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
