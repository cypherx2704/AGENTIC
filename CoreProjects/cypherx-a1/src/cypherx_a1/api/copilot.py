"""Copilot endpoint — ``POST /v1/copilot/ask`` (cited answers over engineering memory)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..copilot.service import CopilotService
from ..core.auth import Principal, query_scopes, require_principal, require_scope
from ..models.api import AskRequest, AskResponse

router = APIRouter(tags=["copilot"])


@router.post("/v1/copilot/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> AskResponse:
    require_scope(principal, query_scopes(), "copilot:ask")
    copilot: CopilotService = request.app.state.copilot
    return await copilot.ask(
        tenant_id=principal.tenant_id,
        agent_jwt=principal.raw_token,
        agent_id=principal.agent_id,
        question=body.question,
        session_id=body.session_id,
        top_k=body.top_k,
    )
