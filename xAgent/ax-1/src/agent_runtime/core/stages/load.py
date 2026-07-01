"""LOAD stage — resolve the agent's runtime config row (Component 3, first stage).

Reads the ``xagent.agents`` runtime row for the caller's agent (identity from the JWT,
never the body — Contract 13) via ``agents_repo.get_agent`` under RLS, and sets it on
``ctx.agent``. If the agent has no runtime configuration row, the task cannot execute:
the stage short-circuits with a ``CONFLICT`` terminal error (the api layer maps that to
HTTP 409 — "agent runtime not configured", per core/errors.py). A row whose ``status``
is not ``active`` (``inactive`` / ``pending_config``) is likewise rejected with a
terminal ``CONFLICT`` error ("Agent is not active.") — tasks run only against an
active runtime (Component 1 status transitions, WP02 enforcement).

This stage also seeds ``ctx.prompt_text`` from the submitted task input so the PRE
-GUARDRAIL stage has the user message to check (the task body is ``{message}``; identity
is NOT in the body). No audit step is written for LOAD — the first-cycle audit trail is
exactly the three user-visible stages (guardrail_check_input, llm_call,
guardrail_check_output).

The runtime row is resolved through the agent-config Valkey READ-THROUGH cache
(``services.agent_config_cache``, WP08) keyed by ``agent_id`` (TTL 5min, config). The
cache FAILS OPEN: a miss, an absent/disabled cache, or any Valkey error falls through to
the RLS-scoped ``agents_repo.get_agent`` DB read, so a cache outage never fails a task
(at worst the read is as slow as the uncached path). PUT ``/v1/agents/{id}/runtime``
busts the key on every config change; the TTL backstops a missed invalidation.
"""

from __future__ import annotations

import structlog

from ...core.config import get_settings
from ...services import agent_config_cache
from ..errors import ErrorCode
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)


class LoadStage(Stage):
    """Load the agent runtime config; 409 CONFLICT if the agent is not configured."""

    name = "LOAD"

    async def run(self, ctx: PipelineContext) -> None:
        # Identity is authoritative from the verified JWT (Contract 13). An api_key
        # principal with no agent_id cannot resolve a runtime row.
        agent_id = ctx.principal.agent_id
        if not agent_id:
            ctx.fail(
                ErrorCode.CONFLICT,
                "No agent runtime is configured for this principal.",
            )
            return

        if ctx.pool is None:
            ctx.fail(ErrorCode.INTERNAL_ERROR, "DB pool is not available.")
            return

        # Read-through the agent-config cache (fail-open to a RLS-scoped DB read on a
        # miss / absent / erroring Valkey). The cache resolves via agents_repo.get_agent,
        # so RLS still scopes the underlying read to this tenant.
        agent = await agent_config_cache.get_runtime(
            deps.get_valkey(),
            ctx.pool,
            get_settings(),
            ctx.principal.tenant_id,
            agent_id,
        )
        if agent is None:
            logger.info("agent_runtime_not_configured", agent_id=agent_id)
            ctx.fail(
                ErrorCode.CONFLICT,
                f"Agent {agent_id} has no runtime configuration.",
            )
            return

        # Status enforcement (WP02): tasks execute ONLY against an 'active' runtime.
        # inactive / pending_config rows are visible (GET/PUT runtime) but not runnable.
        if agent.status != "active":
            logger.info("agent_runtime_not_active", agent_id=agent_id, status=agent.status)
            ctx.fail(ErrorCode.CONFLICT, "Agent is not active.")
            return

        ctx.agent = agent

        # Seed the prompt text from the submitted task input (the public body is
        # {message}; identity never comes from the body). Downstream stages mutate this.
        message = ctx.task.input.get("message") if isinstance(ctx.task.input, dict) else None
        ctx.prompt_text = message if isinstance(message, str) else ""

        # Seed WP12 task parameters from the persisted task row onto the context so the
        # enhancement stages (memory scope / cost budget) read them without depending on the
        # api layer to populate the context. Both are NULL/None when the task carried neither
        # (the basic pipeline is unaffected). session_id is what the async/SSE agent reads.
        ctx.session_id = ctx.task.session_id
        ctx.cost_budget_usd = ctx.task.cost_budget_per_task
