"""Capabilities endpoint (Component 7) — agent capability advertisement.

  * ``GET /v1/capabilities`` — advertise the calling agent's capabilities + the
    first-cycle integration surface (single-agent; no tools / memory / skills / rag).

Flow:
  1. ``require_principal`` verifies the inbound agent JWT; the agent is identified by
     ``principal.agent_id`` (from the JWT — Contract 13), never the body/query.
  2. ``get_agent`` loads the runtime row (RLS-scoped); 404 NOT_FOUND when the runtime
     has not been registered for this agent.
  3. Project the ``AgentRuntime`` into the Component 7 response shape
     ``{agent_id, name, version, capabilities, tools, skills}``. First cycle advertises
     no tools/skills (those slots remain empty until Phases 7/8 land).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db import agents_repo
from ..models.agent import AgentRuntime

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["capabilities"])


@router.get("/capabilities", response_model=None)
async def get_capabilities(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Return the calling agent's advertised capabilities (Component 7 shape)."""
    if not principal.agent_id:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            "The authenticated principal is not an agent (no agent_id claim).",
        )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Agent store is not available.")

    runtime = await agents_repo.get_agent(pool, principal.tenant_id, principal.agent_id)
    if runtime is None:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"No runtime configuration registered for agent {principal.agent_id}.",
        )

    return JSONResponse(content=_capabilities_response(runtime))


def _capabilities_response(runtime: AgentRuntime) -> dict[str, Any]:
    """Project AgentRuntime -> the Component 7 capabilities response.

    First cycle is single-agent with no tool/skill execution: ``tools``/``skills`` mirror
    the agent's configured allow-lists (empty by default) and capabilities come from the
    agent's declared ``capabilities`` array.
    """
    return {
        "agent_id": runtime.agent_id,
        "name": runtime.name,
        "version": runtime.runtime_version,
        "capabilities": runtime.capabilities,
        "tools": runtime.allowed_tools,
        "skills": runtime.allowed_skills,
    }
