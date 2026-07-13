"""END-TO-END against the REAL product MCP server (`mcp-eng-memory`), not a test double.

This is the strongest possible network-free proof: the REAL :class:`ToolLoopStage` + REAL
:class:`McpClient` drive the ACTUAL `mcp_eng_memory` FastAPI app (its real JSON-RPC router,
manifest input-schema validator, dual-scope governance, and tool->backend dispatch) over
`httpx.ASGITransport`. Only two seams are faked — the auth dependency (a fixed Principal) and the
cypherx-a1 backend proxy (`FakeBackend`, canned cited data) — exactly as `mcp-eng-memory`'s own
suite does. The LLM is scripted (a `_FakeLlms` emitting a schema-correct `who_owns` tool call).

`mcp_eng_memory` lives in a separate uv project, so this test only runs when that package is
importable — i.e. under an overlay venv:

    cd xAgent/ax-1
    uv run --with-editable ../../CoreProjects/cypherx-a1/mcp-eng-memory \
        pytest tests/test_tool_loop_real_server_e2e.py -q

In the plain xAgent venv it is skipped (importorskip), so the normal suite is unaffected.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport

# Skip cleanly unless the real product package is importable (overlay venv only).
pytest.importorskip("mcp_eng_memory", reason="mcp_eng_memory not installed; run under uv --with-editable")

# Point the manifest loader at the committed source-of-truth BEFORE importing the app/settings
# (the manifest lives in the mcp-eng-memory project at the repo root).
_MANIFEST = (
    pathlib.Path(__file__).resolve().parents[3]
    / "CoreProjects" / "cypherx-a1" / "mcp-eng-memory" / "manifest.json"
)
os.environ.setdefault("MANIFEST_PATH", str(_MANIFEST))

from mcp_eng_memory.core.auth import Principal as MemPrincipal  # noqa: E402
from mcp_eng_memory.core.auth import require_principal as mem_require_principal  # noqa: E402
from mcp_eng_memory.main import create_app as create_mem_app  # noqa: E402
from mcp_eng_memory.services.manifest import build_manifest  # noqa: E402

from agent_runtime.core.auth import Principal  # noqa: E402
from agent_runtime.core.config import Settings, get_settings  # noqa: E402
from agent_runtime.core.pipeline import PipelineContext  # noqa: E402
from agent_runtime.core.stages import deps  # noqa: E402
from agent_runtime.core.stages.tool_loop import ToolLoopStage  # noqa: E402
from agent_runtime.db.steps_repo import StepBuffer  # noqa: E402
from agent_runtime.db.tasks_repo import TaskRow  # noqa: E402
from agent_runtime.models.agent import AgentRuntime  # noqa: E402
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL  # noqa: E402
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage  # noqa: E402
from agent_runtime.services.mcp_client import McpClient  # noqa: E402
from agent_runtime.services.registry_client import ToolResolution  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"
INVOKE_URL = "http://mcp-eng-memory"


class _FakeBackend:
    """The cypherx-a1 proxy stand-in (same shape as mcp-eng-memory's own test fake)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def graph(self, path: str, body: dict, *, agent_jwt: str) -> dict:
        self.calls.append((path, body))
        return {
            "items": [{"target": body.get("target") or body.get("topic"), "owner": "payments-team"}],
            "citations": [{"kind": "entity", "title": body.get("target") or "acme/payments"}],
        }

    async def ask(self, question: str, *, agent_jwt: str) -> dict:
        self.calls.append(("/v1/copilot/ask", {"question": question}))
        return {"answer": "because X", "citations": [{"kind": "chunk", "title": "PR 101"}]}

    async def aclose(self) -> None:
        pass


class _FakeTokens:
    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        return "svc.jwt.token"

    async def aclose(self) -> None:
        return None


@dataclass
class _FakeRegistry:
    manifest: dict[str, Any]
    resolve_calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        self.resolve_calls.append((name, version))
        return ToolResolution(name=name, version="1.0.0", manifest=self.manifest, invoke_url=INVOKE_URL)

    async def get_tool_access(self, name: str, *, capability: str | None = None, **kw: Any) -> str:
        return "automated"


@dataclass
class _FakeLlms:
    completions: list[ChatCompletion]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    offered: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append([dict(m) for m in messages])
        self.offered.append(kw.get("tools") or [])
        return self.completions.pop(0)


def _completion(*, content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content, finish_reason="tool_calls" if tool_calls else "stop", model="smart",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        tool_calls=tool_calls or [], raw={},
    )


def _settings(**overrides: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings(**base)


def _agent(allowed_tools: list[str]) -> AgentRuntime:
    return AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                        llm_model="smart", allowed_tools=allowed_tools)


def _ctx(agent: AgentRuntime) -> PipelineContext:
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="agent.jwt", trace_id=TRACE_ID, request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": "who owns acme/payments?"}),
        agent=agent, prompt_text="who owns acme/payments?",
        messages=[{"role": "user", "content": "who owns acme/payments?"}],
        steps=StepBuffer(), pool=None, started_monotonic=time.monotonic(), cost_budget_usd=None,
    )


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


async def test_llm_calls_who_owns_on_the_real_mcp_eng_memory_server() -> None:
    """A scripted LLM tool_call → real McpClient → the REAL mcp-eng-memory app → cited result."""
    mem_app = create_mem_app()
    mem_principal = MemPrincipal(
        tenant_id=TENANT, agent_id=AGENT,
        scopes=["tool:invoke", "tool:mcp-eng-memory:invoke"], agent_jwt="agent.jwt",
    )
    mem_app.dependency_overrides[mem_require_principal] = lambda: mem_principal
    backend = _FakeBackend()

    async with LifespanManager(mem_app, startup_timeout=20):
        mem_app.state.backend = backend  # replace the real cypherx-a1 proxy with the fake
        http = httpx.AsyncClient(transport=ASGITransport(app=mem_app), base_url=INVOKE_URL)
        real_mcp = McpClient(_settings(mcp_retry_attempts=0), _FakeTokens(), client=http)

        # The registry hands the stage the REAL manifest (mcp.endpoint=/mcp + the 8 real tools).
        registry = _FakeRegistry(manifest=build_manifest())
        llms = _FakeLlms(completions=[
            _completion(tool_calls=[
                ToolCall(id="call-1", name="who_owns", arguments={"target": "acme/payments"}),
            ]),
            _completion(content="acme/payments is owned by the payments team."),
        ])
        deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
        deps.set_clients(guardrails_client=None, llms_client=llms)
        ctx = _ctx(_agent(["who_owns"]))

        try:
            await ToolLoopStage().run(ctx)
        finally:
            await http.aclose()

    # who_owns was among the tools the REAL manifest offered to the LLM.
    assert "who_owns" in [t["function"]["name"] for t in llms.offered[0]]
    # The REAL server dispatched the call to the real graph endpoint for who_owns.
    assert backend.calls, "the real mcp-eng-memory server must have dispatched to the backend"
    assert backend.calls[0][0] == "/v1/graph/who-owns"
    assert backend.calls[0][1].get("target") == "acme/payments"
    # The real server's CITED result rode back through McpClient and was fed to the LLM.
    tool_msg = next(m for m in llms.calls[1] if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "call-1"
    fed_back = json.loads(tool_msg["content"])
    assert fed_back["result"]["citations"], "the real server's citations must survive the round-trip"
    # The LLM's final answer stands; the tool step passed.
    assert ctx.final_answer == "acme/payments is owned by the payments team."
    tool_steps = [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_TOOL_CALL]
    assert len(tool_steps) == 1 and tool_steps[0].status == "passed"
    assert ctx.terminal_error is None
