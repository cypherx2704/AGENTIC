"""WP12 — RAG_QUERY stage (``core/stages/rag_query.py``).

Drives the REAL :class:`RagQueryStage` against a FAKE :class:`RagClient` injected via the
``deps.set_enhancement_clients`` seam (the same seam the api lifespan wires). No network /
DB: ``ctx.pool`` is None so ``record_step`` is buffer-only.

Coverage:
  * a configured agent (``allowed_kb_ids``) queries EACH KB, stashes chunks on
    ``ctx.rag_chunks``, and writes ONE ``rag_query`` step summarising the KBs;
  * top_k is clamped to ``settings.rag_query_max_top_k`` (≤20) even when the agent asks
    for more;
  * chunks below ``agent.rag_min_score`` are dropped client-side;
  * a 403 forbidden KB is SKIPPED (not fatal) and the rest continue;
  * a transport / non-403 ApiError on one KB is FAIL-SOFT (that KB is skipped, errored);
  * an agent with NO ``allowed_kb_ids`` is a no-op (default-disabled shape): no client
    call, no step, no chunks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.rag_query import RagQueryStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_RAG_QUERY
from agent_runtime.services.rag_client import RagChunk, RagResult

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# ── Fake RagClient (mirrors services.rag_client.RagClient.query) ────────────────────
@dataclass
class _FakeRagClient:
    """Per-KB scripted responses. ``responses[kb_id]`` is a RagResult OR an Exception."""

    responses: dict[str, Any]
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    async def query(
        self, kb_id: str, query: str, top_k: int, *, agent_jwt: str, on_behalf_of: str | None = None
    ) -> RagResult:
        self.calls.append((kb_id, query, top_k))
        outcome = self.responses[kb_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _agent(**overrides: Any) -> AgentRuntime:
    base: dict[str, Any] = {
        "agent_id": AGENT, "tenant_id": TENANT, "name": "A", "system_prompt": "sys",
        "allowed_kb_ids": ["kb-1", "kb-2"], "rag_top_k_per_kb": 5, "rag_min_score": 0.7,
    }
    base.update(overrides)
    return AgentRuntime(**base)


def _ctx(agent: AgentRuntime, prompt: str = "what is x?") -> PipelineContext:
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


@pytest.fixture(autouse=True)
def _unwire_clients() -> Any:
    """Restore the deps client holders after every test (module-global state)."""
    yield
    deps.set_enhancement_clients()


def _rag_step(ctx: PipelineContext) -> Any:
    rag = [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_RAG_QUERY]
    return rag


# ── happy path: query each KB, stash chunks, write one step ─────────────────────────
async def test_queries_each_kb_and_stashes_chunks() -> None:
    fake = _FakeRagClient(responses={
        "kb-1": RagResult(kb_id="kb-1", results=[
            RagChunk(chunk_id="c1", text="alpha", score=0.9, document_id="d1"),
            RagChunk(chunk_id="c2", text="beta", score=0.8),
        ]),
        "kb-2": RagResult(kb_id="kb-2", results=[
            RagChunk(chunk_id="c3", text="gamma", score=0.95, document_id="d3"),
        ]),
    })
    deps.set_enhancement_clients(rag_client=fake)
    ctx = _ctx(_agent())

    await RagQueryStage().run(ctx)

    # Both KBs queried (in config order).
    assert [c[0] for c in fake.calls] == ["kb-1", "kb-2"]
    # All three chunks (all >= min_score) stashed with full provenance.
    assert len(ctx.rag_chunks) == 3
    assert {c["chunk_id"] for c in ctx.rag_chunks} == {"c1", "c2", "c3"}
    assert ctx.rag_chunks[0] == {
        "kb_id": "kb-1", "chunk_id": "c1", "text": "alpha", "score": 0.9, "document_id": "d1"
    }

    steps = _rag_step(ctx)
    assert len(steps) == 1
    step = steps[0]
    assert step.step_name == "rag_query"
    assert step.status == "passed"
    assert step.output["rag_chunks_returned"] == 3
    assert step.output["kbs_queried"] == ["kb-1", "kb-2"]
    assert step.output["kbs_forbidden"] == []
    assert step.output["kbs_errored"] == []


# ── top_k clamp to settings.rag_query_max_top_k (≤20) ───────────────────────────────
async def test_top_k_clamped_to_service_cap() -> None:
    fake = _FakeRagClient(responses={"kb-1": RagResult(kb_id="kb-1", results=[])})
    deps.set_enhancement_clients(rag_client=fake)
    # Agent asks for 100; the RAG service caps at 20 (settings.rag_query_max_top_k default).
    ctx = _ctx(_agent(allowed_kb_ids=["kb-1"], rag_top_k_per_kb=100))

    await RagQueryStage().run(ctx)

    assert fake.calls[0][2] == 20  # clamped, not 100
    assert _rag_step(ctx)[0].output["top_k"] == 20


# ── min_score filter drops low-scoring chunks client-side ───────────────────────────
async def test_min_score_filter_drops_low_chunks() -> None:
    fake = _FakeRagClient(responses={
        "kb-1": RagResult(kb_id="kb-1", results=[
            RagChunk(chunk_id="hi", text="keep", score=0.71),
            RagChunk(chunk_id="lo", text="drop", score=0.69),  # below 0.7 threshold
            RagChunk(chunk_id="edge", text="drop-edge", score=0.7),  # strict <, so 0.7 KEPT
        ]),
    })
    deps.set_enhancement_clients(rag_client=fake)
    ctx = _ctx(_agent(allowed_kb_ids=["kb-1"], rag_min_score=0.7))

    await RagQueryStage().run(ctx)

    kept = {c["chunk_id"] for c in ctx.rag_chunks}
    assert kept == {"hi", "edge"}  # 0.69 dropped; 0.7 kept (filter is score < min_score)
    assert _rag_step(ctx)[0].output["rag_chunks_returned"] == 2


# ── 403 forbidden KB is skipped (not fatal); other KBs continue ─────────────────────
async def test_forbidden_kb_skipped_not_fatal() -> None:
    fake = _FakeRagClient(responses={
        "kb-forbidden": RagResult(kb_id="kb-forbidden", results=[], forbidden=True),
        "kb-ok": RagResult(kb_id="kb-ok", results=[RagChunk(chunk_id="c", text="ok", score=0.9)]),
    })
    deps.set_enhancement_clients(rag_client=fake)
    ctx = _ctx(_agent(allowed_kb_ids=["kb-forbidden", "kb-ok"]))

    await RagQueryStage().run(ctx)

    assert ctx.terminal_error is None  # never fatal
    assert [c["chunk_id"] for c in ctx.rag_chunks] == ["c"]  # only the allowed KB's chunk
    out = _rag_step(ctx)[0].output
    assert out["kbs_forbidden"] == ["kb-forbidden"]
    assert out["kbs_queried"] == ["kb-ok"]


# ── transport / non-403 error on one KB is fail-soft (errored, not fatal) ───────────
async def test_kb_transport_error_is_fail_soft() -> None:
    fake = _FakeRagClient(responses={
        "kb-bad": ApiError(ErrorCode.SERVICE_UNAVAILABLE, "RAG down"),
        "kb-good": RagResult(kb_id="kb-good", results=[RagChunk(chunk_id="g", text="g", score=0.99)]),
    })
    deps.set_enhancement_clients(rag_client=fake)
    ctx = _ctx(_agent(allowed_kb_ids=["kb-bad", "kb-good"]))

    await RagQueryStage().run(ctx)

    assert ctx.terminal_error is None  # a retrieval blip never fails the task
    assert [c["chunk_id"] for c in ctx.rag_chunks] == ["g"]
    out = _rag_step(ctx)[0].output
    assert out["kbs_errored"] == ["kb-bad"]
    assert out["kbs_queried"] == ["kb-good"]


# ── default-disabled shape: no allowed_kb_ids -> no-op (no client call, no step) ────
async def test_no_kbs_is_noop() -> None:
    fake = _FakeRagClient(responses={})
    deps.set_enhancement_clients(rag_client=fake)
    ctx = _ctx(_agent(allowed_kb_ids=[]))

    await RagQueryStage().run(ctx)

    assert fake.calls == []  # the client was never touched
    assert ctx.rag_chunks == []
    assert _rag_step(ctx) == []  # no rag_query step written


async def test_no_agent_is_noop() -> None:
    deps.set_enhancement_clients(rag_client=_FakeRagClient(responses={}))
    ctx = _ctx(_agent())
    ctx.agent = None

    await RagQueryStage().run(ctx)

    assert ctx.rag_chunks == []
    assert _rag_step(ctx) == []
