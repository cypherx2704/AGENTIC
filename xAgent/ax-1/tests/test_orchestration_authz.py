"""Unit tests for the pure orchestration authorization guards (migration 0008)."""

from __future__ import annotations

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.orchestration.authz import (
    AgentHierarchy,
    assert_owns_subagent,
    require_orchestrator,
)

ORCH = "11111111-1111-4111-8111-111111111111"
SUB = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def _principal(agent_type: str, agent_id: str | None = ORCH) -> Principal:
    return Principal(
        tenant_id="t",
        agent_id=agent_id,
        scopes=["agent:execute", "orchestrator:manage"],
        agent_type=agent_type,
    )


# ── require_orchestrator ─────────────────────────────────────────────────────────────────
def test_orchestrator_allowed() -> None:
    assert require_orchestrator(_principal("orchestrator")) == ORCH


def test_sub_agent_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        require_orchestrator(_principal("sub_agent"))
    assert exc.value.status_code == 403
    assert exc.value.details == {"reason": "SUB_AGENT_CANNOT_ORCHESTRATE"}


def test_user_created_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        require_orchestrator(_principal("user_created"))
    assert exc.value.status_code == 403
    assert exc.value.details == {"reason": "ORCHESTRATOR_REQUIRED"}


def test_no_agent_identity_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        require_orchestrator(_principal("orchestrator", agent_id=None))
    assert exc.value.status_code == 403
    assert exc.value.details == {"reason": "NO_AGENT_IDENTITY"}


# ── assert_owns_subagent ─────────────────────────────────────────────────────────────────
def test_owns_subagent_ok() -> None:
    target = AgentHierarchy(agent_id=SUB, agent_type="sub_agent", parent_orchestrator_id=ORCH)
    assert assert_owns_subagent(ORCH, target) is target


@pytest.mark.parametrize(
    "target",
    [
        None,  # RLS-hidden / unknown
        AgentHierarchy(agent_id=SUB, agent_type="sub_agent", parent_orchestrator_id=OTHER),
        AgentHierarchy(agent_id=SUB, agent_type="user_created", parent_orchestrator_id=ORCH),
        AgentHierarchy(agent_id=SUB, agent_type="orchestrator", parent_orchestrator_id=None),
    ],
)
def test_not_owned_is_404_invisible(target: AgentHierarchy | None) -> None:
    with pytest.raises(ApiError) as exc:
        assert_owns_subagent(ORCH, target)
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert exc.value.status_code == 404
