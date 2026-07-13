"""Authorization guards for the orchestration engine (migration 0008) — pure, no I/O.

Enforces the two invariants the internal (non-A2A) orchestrator -> sub-agent path relies on:

  1. **Orchestrator-only.** Only an agent whose ``agent_type == 'orchestrator'`` may drive a
     workflow / spawn sub-agent nodes. This mirrors the auth service's ``requireOrchestrator``
     (a ``sub_agent`` is depth-1 and cannot delegate). The check is on the *verified JWT claim*
     (``Principal.agent_type``), not a request field.
  2. **Owns-its-sub-agent.** An orchestrator may only assign a node to a sub-agent IT OWNS
     (``target.parent_orchestrator_id == orchestrator.agent_id``). A target that is not an
     owned sub-agent is reported as ``NOT_FOUND`` (invisible) — never ``403`` — matching auth's
     404 ownership boundary so agent existence never leaks across the hierarchy.

The caller's hierarchy comes from the JWT; the target's hierarchy is read from the
``xagent.agents`` mirror via :func:`orchestration.repo.get_agent_hierarchy` (RLS-scoped, so a
cross-tenant target is already invisible -> ``None`` -> ``NOT_FOUND``).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.auth import Principal
from ..core.errors import ApiError, ErrorCode

ORCHESTRATOR = "orchestrator"
SUB_AGENT = "sub_agent"


@dataclass(frozen=True)
class AgentHierarchy:
    """The hierarchy facts of an agent (read from the ``xagent.agents`` mirror)."""

    agent_id: str
    agent_type: str
    parent_orchestrator_id: str | None


def require_orchestrator(principal: Principal) -> str:
    """Assert the caller is the tenant orchestrator; return its ``agent_id``.

    Raises ``403 FORBIDDEN`` when the caller is a sub-agent (``SUB_AGENT_CANNOT_ORCHESTRATE``
    — delegation depth is limited to 1), is any other non-orchestrator agent
    (``ORCHESTRATOR_REQUIRED``), or has no agent identity at all (an api_key-only token).
    """
    if principal.agent_id is None:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Orchestration requires an agent identity.",
            details={"reason": "NO_AGENT_IDENTITY"},
        )
    if principal.agent_type == SUB_AGENT:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Sub-agents cannot orchestrate (delegation depth is limited to 1).",
            details={"reason": "SUB_AGENT_CANNOT_ORCHESTRATE"},
        )
    if principal.agent_type != ORCHESTRATOR:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Only the tenant orchestrator can run sub-agent workflows.",
            details={"reason": "ORCHESTRATOR_REQUIRED"},
        )
    return principal.agent_id


def assert_owns_subagent(orchestrator_id: str, target: AgentHierarchy | None) -> AgentHierarchy:
    """Assert ``target`` is a sub-agent owned by ``orchestrator_id``; else ``404 NOT_FOUND``.

    ``None`` (RLS-hidden / cross-tenant / unknown), a non-sub-agent, and a sub-agent owned by
    a *different* orchestrator ALL raise ``404`` (invisible) — never ``403`` — so the caller
    cannot probe for the existence of agents outside its own sub-tree.
    """
    if (
        target is None
        or target.agent_type != SUB_AGENT
        or target.parent_orchestrator_id != orchestrator_id
    ):
        raise ApiError(ErrorCode.NOT_FOUND, "Sub-agent not found.")
    return target
