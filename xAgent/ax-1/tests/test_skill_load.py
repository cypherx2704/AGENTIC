"""Phase 8 — SKILL_LOAD stage (``core/stages/skill_load.py``).

Drives the REAL :class:`SkillLoadStage` against a FAKE SkillRegistryClient injected via the
deps seam. No network / DB. Covers the per-agent access gate (none -> dropped), fail-soft
on resolve errors / an unwired client, and the PROMPT_BUILD splice of resolved skills.
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
from agent_runtime.core.stages.prompt_build import PromptBuildStage
from agent_runtime.core.stages.skill_load import SkillLoadStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.skill_registry_client import SkillResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"


@dataclass
class _FakeSkillRegistry:
    resolutions: dict[str, Any]  # name -> SkillResolution | Exception
    access: dict[str, str]  # name -> none|ask|automated
    access_calls: list[str] = field(default_factory=list)

    async def resolve_skill(self, name: str, version: str | None = None, **kw: Any) -> SkillResolution:
        out = self.resolutions[name]
        if isinstance(out, Exception):
            raise out
        return out

    async def get_skill_access(self, name: str, **kw: Any) -> str:
        self.access_calls.append(name)
        return self.access.get(name, "automated")


def _skill(name: str, desc: str, version: str = "1.0.0") -> SkillResolution:
    return SkillResolution(name=name, version=version, manifest={"description": desc})


def _agent(allowed_skills: list[str]) -> AgentRuntime:
    return AgentRuntime(
        agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
        allowed_skills=allowed_skills, llm_model="smart",
    )


def _ctx(agent: AgentRuntime) -> PipelineContext:
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="jwt",
        trace_id="22222222-2222-2222-2222-222222222222",
        request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT,
                     trace_id="t", status="running", input={"message": "go"}),
        agent=agent,
        prompt_text="go",
        messages=[{"role": "user", "content": "go"}],
        steps=StepBuffer(),
        pool=None,
        started_monotonic=time.monotonic(),
    )


@pytest.fixture(autouse=True)
def _reset_deps():
    yield
    deps.set_enhancement_clients()  # unbind all enhancement clients after each test


@pytest.mark.asyncio
async def test_skill_load_access_gate_drops_none_keeps_automated() -> None:
    fake = _FakeSkillRegistry(
        resolutions={"summarize": _skill("summarize", "Condense text"),
                     "pay": _skill("pay", "Move money")},
        access={"summarize": "automated", "pay": "none"},
    )
    deps.set_enhancement_clients(skill_registry_client=fake)
    ctx = _ctx(_agent(["summarize", "pay"]))
    await SkillLoadStage().run(ctx)
    assert [s["name"] for s in ctx.skills] == ["summarize"]  # 'pay' (none) dropped
    assert ctx.skills[0]["description"] == "Condense text"
    assert ctx.skills[0]["access_mode"] == "automated"
    assert ctx.terminal_error is None


@pytest.mark.asyncio
async def test_skill_load_failsoft_on_unresolvable_skill() -> None:
    fake = _FakeSkillRegistry(
        resolutions={"ghost": ApiError(ErrorCode.NOT_FOUND, "nope")}, access={},
    )
    deps.set_enhancement_clients(skill_registry_client=fake)
    ctx = _ctx(_agent(["ghost"]))
    await SkillLoadStage().run(ctx)
    assert ctx.skills == []
    assert ctx.terminal_error is None  # fail-soft: never fails the task


@pytest.mark.asyncio
async def test_skill_load_failsoft_when_client_unwired() -> None:
    deps.set_enhancement_clients()  # no skill client bound
    ctx = _ctx(_agent(["summarize"]))
    await SkillLoadStage().run(ctx)
    assert ctx.skills == []
    assert ctx.terminal_error is None


@pytest.mark.asyncio
async def test_skill_load_noop_without_allowed_skills() -> None:
    fake = _FakeSkillRegistry(resolutions={}, access={})
    deps.set_enhancement_clients(skill_registry_client=fake)
    ctx = _ctx(_agent([]))
    await SkillLoadStage().run(ctx)
    assert ctx.skills == []
    assert fake.access_calls == []  # never consulted the registry


@pytest.mark.asyncio
async def test_prompt_build_splices_resolved_skills() -> None:
    ctx = _ctx(_agent([]))
    ctx.skills = [{"name": "summarize", "description": "Condense text", "access_mode": "automated"}]
    await PromptBuildStage().run(ctx)
    system = "\n".join(m["content"] for m in ctx.messages if m["role"] == "system")
    assert "Available skills:" in system
    assert "summarize — Condense text" in system
