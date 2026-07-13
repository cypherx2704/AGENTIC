"""WP12 — MEMORY_RETRIEVE + MEMORY_WRITE stages.

Drives the REAL :class:`MemoryRetrieveStage` / :class:`MemoryWriteStage` against a FAKE
:class:`MemoryClient` injected via ``deps.set_enhancement_clients``. No network / DB.

Coverage:
  RETRIEVE
    * agent/tenant scope -> search scoped by ``memory_scope`` (no session_id), stashes
      memories on ``ctx.memories``, writes a ``memory_retrieve`` step;
    * session scope -> search scoped by ``session_id``; a session scope with NO session_id
      degrades to a no-op (no call, no step);
    * a client ApiError is FAIL-SOFT (no memories, step status ``failed``, never fatal);
    * ``memory_scope='none'`` -> no-op.
  WRITE
    * stores the user+assistant interaction when enabled + a final answer exists;
    * never writes when there is a terminal error / no final answer / write disabled /
      scope none / session-scope w/o session_id;
    * a store ApiError is FAIL-SOFT (step ``failed``, ``stored=False``, never fatal).
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
from agent_runtime.core.stages.memory_retrieve import MemoryRetrieveStage
from agent_runtime.core.stages.memory_write import MemoryWriteStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_MEMORY_RETRIEVE, STEP_TYPE_MEMORY_WRITE
from agent_runtime.services.memory_client import MemoryItem, MemorySearchResult, MemoryStoreResult

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# ── Fake MemoryClient (mirrors services.memory_client.MemoryClient) ─────────────────
@dataclass
class _FakeMemoryClient:
    search_result: Any = None  # MemorySearchResult OR Exception
    store_result: Any = None  # MemoryStoreResult OR Exception
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    store_calls: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, query: str, top_k: int, **kw: Any) -> MemorySearchResult:
        self.search_calls.append({"query": query, "top_k": top_k, **kw})
        if isinstance(self.search_result, Exception):
            raise self.search_result
        return self.search_result or MemorySearchResult(results=[])

    async def store(self, content: str, **kw: Any) -> MemoryStoreResult:
        self.store_calls.append({"content": content, **kw})
        if isinstance(self.store_result, Exception):
            raise self.store_result
        return self.store_result or MemoryStoreResult(id=None)


def _agent(scope: str = "agent") -> AgentRuntime:
    return AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s", memory_scope=scope)


def _ctx(agent: AgentRuntime, *, prompt: str = "hi", session_id: str | None = None,
         final_answer: str | None = None,
         scopes: list[str] | None = None) -> PipelineContext:
    # The Memory service authorizes a WRITE against the forwarded AGENT jwt, so the agent must
    # carry `mem:write` — MEMORY_WRITE now skips (loudly) rather than burning a guaranteed 403.
    ctx = PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT,
                            scopes=scopes if scopes is not None else ["agent:execute", "mem:write"],
                            raw_token="jwt"),
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
        session_id=session_id,
    )
    ctx.final_answer = final_answer
    return ctx


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()


def _steps(ctx: PipelineContext, step_type: str) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == step_type]


# ── RETRIEVE ─────────────────────────────────────────────────────────────────────────
async def test_retrieve_agent_scope_stashes_memories() -> None:
    fake = _FakeMemoryClient(search_result=MemorySearchResult(results=[
        MemoryItem(id="m1", content="remembered fact", score=0.9),
        MemoryItem(id="m2", content="another", score=0.8),
    ]))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"))

    await MemoryRetrieveStage().run(ctx)

    # Scoped by memory_scope; agent scope carries NO session_id.
    assert fake.search_calls[0]["scope"] == "agent"
    assert fake.search_calls[0]["session_id"] is None
    assert [m["id"] for m in ctx.memories] == ["m1", "m2"]
    step = _steps(ctx, STEP_TYPE_MEMORY_RETRIEVE)[0]
    assert step.status == "passed"
    assert step.output == {"memories_retrieved": 2, "scope": "agent"}


async def test_retrieve_session_scope_passes_session_id() -> None:
    fake = _FakeMemoryClient(search_result=MemorySearchResult(results=[
        MemoryItem(id="s1", content="session mem", score=0.95),
    ]))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("session"), session_id="sess-abc")

    await MemoryRetrieveStage().run(ctx)

    assert fake.search_calls[0]["scope"] == "session"
    assert fake.search_calls[0]["session_id"] == "sess-abc"
    assert [m["id"] for m in ctx.memories] == ["s1"]


async def test_retrieve_session_scope_without_session_id_is_noop() -> None:
    fake = _FakeMemoryClient(search_result=MemorySearchResult(results=[]))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("session"), session_id=None)  # no correlator

    await MemoryRetrieveStage().run(ctx)

    assert fake.search_calls == []  # nothing to scope to -> no call
    assert ctx.memories == []
    assert _steps(ctx, STEP_TYPE_MEMORY_RETRIEVE) == []  # no step


async def test_retrieve_scope_none_is_noop() -> None:
    fake = _FakeMemoryClient(search_result=MemorySearchResult(results=[]))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("none"))

    await MemoryRetrieveStage().run(ctx)

    assert fake.search_calls == []
    assert _steps(ctx, STEP_TYPE_MEMORY_RETRIEVE) == []


async def test_retrieve_client_error_is_fail_soft() -> None:
    fake = _FakeMemoryClient(search_result=ApiError(ErrorCode.SERVICE_UNAVAILABLE, "memory down"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"))

    await MemoryRetrieveStage().run(ctx)

    assert ctx.terminal_error is None  # fail-soft: never fatal
    assert ctx.memories == []
    step = _steps(ctx, STEP_TYPE_MEMORY_RETRIEVE)[0]
    assert step.status == "failed"
    assert step.output["memories_retrieved"] == 0


# ── WRITE ─────────────────────────────────────────────────────────────────────────────
async def test_write_stores_interaction_when_enabled() -> None:
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="stored-1"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"), prompt="what is 2+2?", final_answer="4")

    await MemoryWriteStage().run(ctx)

    assert len(fake.store_calls) == 1
    content = fake.store_calls[0]["content"]
    assert "User: what is 2+2?" in content
    assert "Assistant: 4" in content
    assert fake.store_calls[0]["scope"] == "agent"
    assert fake.store_calls[0]["metadata"] == {"task_id": TASK_ID}
    step = _steps(ctx, STEP_TYPE_MEMORY_WRITE)[0]
    assert step.status == "passed"
    assert step.output == {"memory_id": "stored-1", "scope": "agent", "stored": True}


async def test_write_session_scope_passes_session_id() -> None:
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="s"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("session"), session_id="sess-9", final_answer="answer")

    await MemoryWriteStage().run(ctx)

    assert fake.store_calls[0]["session_id"] == "sess-9"


async def test_write_skipped_when_no_final_answer() -> None:
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="x"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"), final_answer=None)  # answer-less interaction

    await MemoryWriteStage().run(ctx)

    assert fake.store_calls == []  # never store a memory of an answer-less interaction
    assert _steps(ctx, STEP_TYPE_MEMORY_WRITE) == []


async def test_write_skipped_on_terminal_error() -> None:
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="x"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"), final_answer="blocked answer")
    ctx.fail(ErrorCode.GUARDRAIL_VIOLATION, "blocked")  # short-circuited

    await MemoryWriteStage().run(ctx)

    assert fake.store_calls == []  # never store a memory of a blocked interaction


async def test_write_skipped_when_session_scope_without_session_id() -> None:
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="x"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("session"), session_id=None, final_answer="answer")

    await MemoryWriteStage().run(ctx)

    assert fake.store_calls == []


async def test_write_client_error_is_fail_soft() -> None:
    fake = _FakeMemoryClient(store_result=ApiError(ErrorCode.SERVICE_UNAVAILABLE, "store down"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"), final_answer="answer")

    await MemoryWriteStage().run(ctx)

    assert ctx.terminal_error is None  # a memory-write blip never fails the task
    step = _steps(ctx, STEP_TYPE_MEMORY_WRITE)[0]
    assert step.status == "failed"
    assert step.output["stored"] is False


# ── the missing-scope guard (silent-403 regression) ─────────────────────────────────────
async def test_write_skips_when_agent_lacks_mem_write_scope() -> None:
    """memory_scope != 'none' WITHOUT the `mem:write` scope is a permanently broken pair: the
    Memory service authorizes writes against the forwarded AGENT jwt, so EVERY task would burn a
    guaranteed 403 — silently, because this stage is fail-soft. It must skip up front instead.
    """
    fake = _FakeMemoryClient(store_result=MemoryStoreResult(id="stored-1"))
    deps.set_enhancement_clients(memory_client=fake)
    ctx = _ctx(_agent("agent"), final_answer="the answer", scopes=["agent:execute"])  # no mem:write

    await MemoryWriteStage().run(ctx)

    assert fake.store_calls == []  # no guaranteed-403 call was made
    assert _steps(ctx, STEP_TYPE_MEMORY_WRITE) == []  # skipped, not "failed"
    assert ctx.terminal_error is None
