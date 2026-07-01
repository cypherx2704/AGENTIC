"""Agent runtime-config lifecycle endpoints (Component 1, WP08).

  * ``GET /v1/agents/{agent_id}/runtime``  — return the agent's runtime config + status
    + runtime_version (404 NOT_FOUND when no runtime row exists for this tenant).
  * ``PUT /v1/agents/{agent_id}/runtime``  — upsert the runtime config with VALIDATED
    status transitions, a runtime_version BUMP on each change, and agent-config cache
    INVALIDATION. Create on first write; update (transition + bump) thereafter.
  * ``POST /v1/agents/{agent_id}/runtime`` — RETAINED for back-compat: create-only
    (idempotent ON CONFLICT DO NOTHING). A duplicate POST returns the existing row
    UNCHANGED — use PUT to modify an existing runtime.

Common flow (all three):
  1. ``require_principal`` verifies the inbound agent JWT and yields identity. The caller
     MUST hold an admin scope (``agent:admin`` or ``platform:admin``); otherwise 403
     FORBIDDEN. Identity (tenant_id) is read ONLY from the JWT (Contract 13). The runtime
     management surface is admin-only; an agent reads its OWN advertised config via
     ``GET /v1/capabilities`` (Component 7), not this endpoint.
  2. (writes) The target agent is cross-validated against Auth ``GET /v1/agents/{id}``: it
     must exist (else 404 NOT_FOUND) and its tenant must match the caller's JWT tenant
     (else 403 FORBIDDEN). Auth identity is the source of truth for an agent's tenant.

Status transitions (PUT): ``pending_config -> {active, inactive}``; ``active <-> inactive``;
self-transitions (X -> X) always allowed; regressing to ``pending_config`` is rejected
(409 CONFLICT). See ``models.agent.is_valid_status_transition``.

runtime_version bump (PUT update): the PATCH component is incremented on every successful
update (``1.0.0 -> 1.0.1``; non-semver/free-text versions are left untouched) so each
config change is provenance-stamped. CREATE keeps the body's version (default ``1.0.0``).

Cache invalidation: a successful PUT (create or update) busts the agent-config Valkey
cache key for ``agent_id`` so the next task's LOAD stage re-reads the fresh config (the
TTL backstops a missed bust). Fail-soft — an absent/erroring Valkey never fails the PUT.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.auth import Principal, require_principal
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..core.validation import parse_uuid_path
from ..db import agents_repo
from ..models.agent import (
    AgentRuntime,
    AgentRuntimeRegistration,
    bump_runtime_version,
    is_valid_status_transition,
)
from ..services import agent_config_cache
from ..services.auth_client import AuthClient

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["agents"])

# Scopes permitted to read/register/replace an agent's runtime config (admin surface).
_ADMIN_SCOPES = frozenset({"agent:admin", "platform:admin"})


def _require_admin(principal: Principal) -> None:
    """Gate the runtime-management surface on an admin scope (403 otherwise)."""
    if _ADMIN_SCOPES.isdisjoint(principal.scopes):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Managing an agent runtime requires the 'agent:admin' or 'platform:admin' scope.",
        )


def _require_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Agent store is not available.")
    return pool


def _auth_client(request: Request) -> AuthClient:
    """Return the shared AuthClient, building one lazily from app.state if absent.

    The foundation lifespan sets ``app.state.token_provider`` (the shared service-token
    provider). If a dedicated ``app.state.auth_client`` was wired we reuse it; otherwise
    we construct one over the shared provider so identity headers stay consistent.
    """
    existing = getattr(request.app.state, "auth_client", None)
    if existing is not None:
        return existing
    settings = getattr(request.app.state, "settings", None) or get_settings()
    token_provider = request.app.state.token_provider
    client = AuthClient(settings, token_provider)
    request.app.state.auth_client = client  # cache for reuse + lifespan-less teardown
    return client


async def _cross_validate_agent(request: Request, principal: Principal, agent_id: str) -> None:
    """Confirm the target agent exists in Auth AND belongs to the caller's tenant.

    404 NOT_FOUND when Auth has no such agent; 403 FORBIDDEN on a tenant mismatch (the
    caller is authenticated, so a mismatch is a genuine cross-tenant attempt — we do not
    leak that the agent exists elsewhere beyond the FORBIDDEN). Enforced on writes only.
    """
    auth_client = _auth_client(request)
    auth_agent = await auth_client.get_agent(
        agent_id,
        agent_jwt=principal.raw_token,
        on_behalf_of=principal.agent_id,
    )
    if auth_agent.tenant_id != principal.tenant_id:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Agent belongs to a different tenant than the caller.",
        )


# ── GET /v1/agents/{agent_id}/runtime ──────────────────────────────────────────────
@router.get("/agents/{agent_id}/runtime", response_model=None)
async def get_runtime(
    agent_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Return the runtime config (+ status + runtime_version) for ``agent_id``."""
    _require_admin(principal)
    # Validate the path UUID BEFORE the repo binds it to the agents.agent_id ``uuid`` column
    # (BUG 1) — a non-UUID id cannot name any row -> 404, never an uncastable-value 5xx.
    agent_id = parse_uuid_path(agent_id, param="Agent")
    pool = _require_pool(request)

    runtime = await agents_repo.get_agent(pool, principal.tenant_id, agent_id)
    if runtime is None:
        # RLS hides cross-tenant rows -> they surface as NOT_FOUND (never leak existence).
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"No runtime configuration registered for agent {agent_id}.",
        )
    return JSONResponse(content=_runtime_to_dict(runtime))


# ── PUT /v1/agents/{agent_id}/runtime ──────────────────────────────────────────────
@router.put("/agents/{agent_id}/runtime", response_model=None)
async def put_runtime(
    agent_id: str,
    body: AgentRuntimeRegistration,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Upsert the runtime config: create on first write, transition+bump thereafter."""
    _require_admin(principal)
    # Validate the path UUID BEFORE the Auth cross-validate / repo bind (BUG 1 + MINOR): a
    # non-UUID id used to reach Auth GET /v1/agents/{bad} and surface as a misleading 503
    # (cross-validate treated the bad id as a downstream failure). Now a clean 404.
    agent_id = parse_uuid_path(agent_id, param="Agent")
    pool = _require_pool(request)

    # Writes cross-validate the target against Auth identity (existence + tenant).
    await _cross_validate_agent(request, principal, agent_id)

    existing = await agents_repo.get_agent(pool, principal.tenant_id, agent_id)

    if existing is None:
        # CREATE path: insert the row as supplied (status + version come from the body).
        created = await agents_repo.insert_agent_runtime(pool, principal.tenant_id, agent_id, body)
        await _invalidate_cache(request, agent_id)
        logger.info(
            "agent_runtime_created",
            agent_id=agent_id,
            status=created.status,
            runtime_version=created.runtime_version,
        )
        return JSONResponse(content=_runtime_to_dict(created), status_code=201)

    # UPDATE path: validate the status transition, then bump the version + write.
    if not is_valid_status_transition(existing.status, body.status):
        raise ApiError(
            ErrorCode.CONFLICT,
            f"Invalid status transition: {existing.status!r} -> {body.status!r}.",
            details={
                "reason": "INVALID_STATUS_TRANSITION",
                "from": existing.status,
                "to": body.status,
            },
        )

    new_version = bump_runtime_version(existing.runtime_version)
    updated = await agents_repo.update_agent_runtime(
        pool,
        principal.tenant_id,
        agent_id,
        body,
        runtime_version=new_version,
        status=body.status,
    )
    if updated is None:
        # The row vanished between the read and the write (RLS / concurrent delete).
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"No runtime configuration registered for agent {agent_id}.",
        )

    await _invalidate_cache(request, agent_id)
    logger.info(
        "agent_runtime_updated",
        agent_id=agent_id,
        status=updated.status,
        runtime_version=updated.runtime_version,
        previous_version=existing.runtime_version,
    )
    return JSONResponse(content=_runtime_to_dict(updated))


# ── POST /v1/agents/{agent_id}/runtime (back-compat: create-only, idempotent) ───────
@router.post("/agents/{agent_id}/runtime", response_model=None)
async def register_runtime(
    agent_id: str,
    body: AgentRuntimeRegistration,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    """Register (idempotently) the runtime config for ``agent_id`` (create-only).

    RETAINED for back-compat with the original Component-1 step-2 contract: ON CONFLICT
    DO NOTHING then re-select, so a duplicate registration returns the EXISTING row
    UNCHANGED. To MODIFY an existing runtime (status transition / version bump), use PUT.
    """
    _require_admin(principal)
    # Validate the path UUID BEFORE the Auth cross-validate / repo bind (BUG 1 + MINOR) —
    # a non-UUID id is a clean 404, never the misleading downstream-failure 503.
    agent_id = parse_uuid_path(agent_id, param="Agent")
    pool = _require_pool(request)

    await _cross_validate_agent(request, principal, agent_id)

    runtime: AgentRuntime = await agents_repo.upsert_agent_runtime(
        pool,
        principal.tenant_id,
        agent_id,
        body,
    )
    # Bust the cache so a first-create is immediately visible to LOAD (a no-op on a
    # duplicate POST, which returned the unchanged existing row).
    await _invalidate_cache(request, agent_id)
    return JSONResponse(content=_runtime_to_dict(runtime))


async def _invalidate_cache(request: Request, agent_id: str) -> None:
    """Bust the agent-config Valkey cache for ``agent_id`` (fail-soft; TTL backstops)."""
    valkey = getattr(request.app.state, "valkey", None)
    await agent_config_cache.invalidate(valkey, get_settings(), agent_id)


def _runtime_to_dict(runtime: AgentRuntime) -> dict[str, Any]:
    """Serialise the AgentRuntime to a JSON-ready dict (config + identity view)."""
    return runtime.model_dump(mode="json")
