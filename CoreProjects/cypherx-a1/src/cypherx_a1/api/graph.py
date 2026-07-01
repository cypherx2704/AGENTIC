"""Graph query surface — ``/v1/graph/*``.

The read-only, cited, no-LLM query endpoints that answer the core engineering-memory
questions. These are ALSO the backing API the stateless ``mcp-eng-memory`` server proxies,
so the same logic serves both the public REST API and autonomous coding agents over MCP.
All endpoints require the query scope; identity comes only from the JWT.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..copilot.queries import GraphQueryService
from ..core import trace
from ..core.auth import Principal, query_scopes, require_principal, require_scope
from ..models.api import ActivityRequest, GraphAnswer, TargetRequest, TopicRequest, WhatBreaksRequest

router = APIRouter(tags=["graph"], prefix="/v1/graph")


def _svc(request: Request) -> GraphQueryService:
    return request.app.state.graph_queries


@router.post("/who-owns", response_model=GraphAnswer)
async def who_owns(
    body: TargetRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:who-owns")
    items, citations = await _svc(request).who_owns(tenant_id=principal.tenant_id, target=body.target)
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())


@router.post("/what-breaks", response_model=GraphAnswer)
async def what_breaks(
    body: WhatBreaksRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:what-breaks")
    items, citations = await _svc(request).what_breaks_if_changed(
        tenant_id=principal.tenant_id, target=body.target, max_hops=body.max_hops
    )
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())


@router.post("/experts", response_model=GraphAnswer)
async def experts(
    body: TopicRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:experts")
    items, citations = await _svc(request).experts_on(tenant_id=principal.tenant_id, topic=body.topic)
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())


@router.post("/why-built", response_model=GraphAnswer)
async def why_built(
    body: TopicRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:why-built")
    items, citations = await _svc(request).why_built(tenant_id=principal.tenant_id, feature=body.topic)
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())


@router.post("/activity", response_model=GraphAnswer)
async def activity(
    body: ActivityRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:activity")
    items, citations = await _svc(request).activity(
        tenant_id=principal.tenant_id, target=body.target, since=body.since, until=body.until
    )
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())


@router.post("/neighbors", response_model=GraphAnswer)
async def neighbors(
    body: WhatBreaksRequest, request: Request, principal: Principal = Depends(require_principal)
) -> GraphAnswer:
    require_scope(principal, query_scopes(), "graph:neighbors")
    items, citations = await _svc(request).neighbors(
        tenant_id=principal.tenant_id, target=body.target, hops=body.max_hops, as_of=body.as_of
    )
    return GraphAnswer(items=items, citations=citations, trace_id=trace.trace_id_var.get())
