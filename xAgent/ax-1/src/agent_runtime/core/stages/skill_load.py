"""SKILL_LOAD stage — resolve the agent's allowed skills + enforce per-agent access (Phase 8).

Runs BEFORE PROMPT_BUILD (registry slot ``SKILL_LOAD``). TRIGGER: registry-disabled by
default; even when ``STAGE_ENABLE_SKILL_LOAD`` is on, the stage SKIPS unless the agent's
runtime config lists ``allowed_skills``.

Skills are DECLARATIVE capabilities (instructions/templates indexed into RAG), not MCP
servers — so unlike the tool loop, SKILL_LOAD does not INVOKE anything. It resolves each
allowed skill's manifest from the Skill Registry and applies the SAME per-agent access
gate as the tool loop (``none`` | ``ask`` | ``automated``):

  * ``none``                 -> the skill is DROPPED (not offered to the model).
  * ``ask`` / ``automated``  -> the skill is INCLUDED; PROMPT_BUILD splices its name +
    description into the prompt's "Available skills" context block.

FAIL-SOFT: skills are an enhancement, so any registry error (or an unwired client) skips
the affected skill (or the whole stage) and the task proceeds — it never fails the task.
"""

from __future__ import annotations

import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_SKILL_LOAD
from ..errors import ApiError
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)


def _split_pin(entry: str) -> tuple[str, str | None]:
    """Split an ``allowed_skills`` entry ``name`` | ``name@version`` into (name, version)."""
    if "@" in entry:
        name, _, version = entry.partition("@")
        return name.strip(), (version.strip() or None)
    return entry.strip(), None


class SkillLoadStage(Stage):
    """Resolve allowed skills, drop access=none, stash the rest for PROMPT_BUILD."""

    name = "SKILL_LOAD"

    async def run(self, ctx: PipelineContext) -> None:
        agent = ctx.agent
        if agent is None or not agent.allowed_skills:
            return  # no skills configured -> nothing to load

        try:
            client = deps.get_skill_registry_client()
        except ApiError as exc:
            # Unwired client (config) -> fail-soft: skills are an enhancement, never fatal.
            logger.warning("skill_load_client_unavailable", task_id=ctx.task.task_id, error=exc.message)
            return

        loaded: list[dict[str, str]] = []
        for entry in agent.allowed_skills:
            name, pinned = _split_pin(entry)
            if not name:
                continue
            try:
                res = await client.resolve_skill(
                    name, pinned, agent_jwt=ctx.inbound_agent_jwt, on_behalf_of=ctx.principal.agent_id
                )
            except ApiError as exc:
                logger.warning(
                    "skill_resolve_failed", task_id=ctx.task.task_id, skill=entry, error=exc.message
                )
                continue
            # Version-pin enforcement (mirror the tool loop): a pinned entry must match exactly.
            if pinned is not None and res.version and res.version != pinned:
                logger.warning(
                    "skill_version_pin_mismatch", task_id=ctx.task.task_id, skill=name,
                    pinned=pinned, resolved=res.version,
                )
                continue
            mode = await client.get_skill_access(
                name, agent_jwt=ctx.inbound_agent_jwt, on_behalf_of=ctx.principal.agent_id
            )
            if mode == "none":
                logger.info("skill_access_denied", task_id=ctx.task.task_id, skill=name)
                continue
            loaded.append({"name": res.name or name, "description": res.description, "access_mode": mode})

        ctx.skills = loaded
        await self._record_step(ctx, loaded)

    @staticmethod
    async def _record_step(ctx: PipelineContext, loaded: list[dict[str, str]]) -> None:
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_SKILL_LOAD,
                step_name="skill_load",
                status="passed",
                duration_ms=0,
                output={"skills": [s["name"] for s in loaded], "count": len(loaded)},
            ),
        )
