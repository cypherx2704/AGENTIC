"""Tenant-defined LLM rules API (Phase 4) — ``/v1/llm-rules``.

The tenant owner (tenant:admin) declares which (provider, model) pairs are allowed/blocked, whether
agents may use them, and whether their usage is billed (billing_bypass for user-added models). These
rules are the per-tenant "ultimate truth" enforced in the chat path (services.user_llm_rules).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..services import user_llm_rules

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["llm-rules"])

_ADMIN_SCOPES = ("tenant:admin", "platform:admin")


def _require_admin(principal: Principal) -> None:
    if not any(s in principal.scopes for s in _ADMIN_SCOPES):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Managing LLM rules requires tenant:admin.",
            details={"required_any": list(_ADMIN_SCOPES)},
        )


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Rules store is not available.")
    return pool


class RuleBody(BaseModel):
    provider: str
    model_id: str
    rule_type: str = "allow"  # allow | block
    can_be_used_by_agents: bool = True
    billing_bypass: bool = False
    is_user_added: bool = True


@router.get("/llm-rules")
async def list_llm_rules(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(principal)
    pool = _get_pool(request)
    return {"data": await user_llm_rules.list_rules(pool, principal.tenant_id)}


@router.post("/llm-rules", status_code=201)
async def create_llm_rule(
    request: Request,
    body: RuleBody,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    if body.rule_type not in ("allow", "block"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "rule_type must be 'allow' or 'block'.", status_code=422)
    pool = _get_pool(request)
    return await user_llm_rules.upsert_rule(
        pool,
        principal.tenant_id,
        principal.agent_id or principal.tenant_id,
        provider=body.provider.strip(),
        model_id=body.model_id.strip(),
        rule_type=body.rule_type,
        can_be_used_by_agents=body.can_be_used_by_agents,
        billing_bypass=body.billing_bypass,
        is_user_added=body.is_user_added,
    )


@router.delete("/llm-rules/{rule_id}", status_code=204)
async def delete_llm_rule(
    request: Request,
    rule_id: str,
    principal: Principal = Depends(require_principal),
) -> None:
    _require_admin(principal)
    pool = _get_pool(request)
    await user_llm_rules.delete_rule(pool, principal.tenant_id, rule_id)
