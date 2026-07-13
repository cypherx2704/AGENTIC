"""Tool discovery + registration API (WP11).

Discovery (authenticated, any tenant principal):
  * ``GET /v1/tools``         — UNION of platform + the caller's tenant tools, with
    tenant-priority shadowing; each entry resolves its latest active version's
    manifest, invoke URL, and required scopes.
  * ``GET /v1/tools/{name}``  — one tool by name (tenant shadows platform); optional
    ``?version=`` pins a specific active version, else the latest active is resolved.

Registration (scope ``tool:admin`` or ``platform:admin``):
  * ``POST /v1/tools``                 — register a NEW tenant tool + first version.
  * ``POST /v1/tools/{name}/versions`` — append a version; retention keeps max N
    active versions (oldest retired).

The tenant is taken ONLY from the JWT Principal (Contract 13). Manifests are validated
against the Contract-4 shape before any write. A freshly-registered tool/version is
polled EAGERLY so its health is known immediately.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, Query, Request
from psycopg.errors import UniqueViolation

from ..core.auth import ADMIN_SCOPES, Principal, require_principal, require_scopes
from ..core.errors import ApiError, ErrorCode
from ..db import queries
from ..services import discovery
from ..services import manifest as manifest_svc
from ..services.health_poll import HealthState, HttpClient

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["tools"])

require_admin = require_scopes(ADMIN_SCOPES)
# Tool ACCESS management (per-agent access modes, restricted tools) is a tenant-owner action.
require_tenant_admin = require_scopes(("tenant:admin", "platform:admin"))
# PLATFORM (public) registration + platform-tool retirement is a platform-operator action.
require_platform_admin = require_scopes(("platform:admin",))

_ACCESS_MODES = ("none", "ask", "automated")
# Marketplace visibility labels (mirrors the tools.tools CHECK constraint). A label the API
# filters on — NOT an RLS boundary. `public` == the platform (tenant_id NULL) rows.
_VISIBILITY_VALUES = ("private", "protected", "public")


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Tool registry store is not available.")
    return pool


def _manifest_capabilities(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """(capability, required_scope) rows derived from the manifest's tools[]."""
    server_name = manifest["name"]
    fine_scope = f"tool:{server_name}:invoke"
    return [(cap, fine_scope) for cap in manifest_svc.declared_capabilities(manifest)]


def _resolve_visibility(manifest: dict[str, Any], *, is_platform: bool) -> str:
    """Resolve a registration's Marketplace visibility (``private``|``protected``|``public``).

    Platform registrations (tenant_id NULL) are always ``public`` — public rows ARE the
    platform rows. Otherwise take ``manifest['visibility']`` when present (validated against
    the allowed set), defaulting to ``private``.

    GOVERNANCE INVARIANT: ``public`` is reserved for platform rows only — a tenant cannot
    self-declare a tool public. Public is reached exclusively by an admin promotion into the
    platform (tenant_id NULL) namespace, never via a tenant's own registration. A tenant
    registration that requests ``public`` is rejected (not silently downgraded) so the caller
    learns the correct path. A DB CHECK (``visibility <> 'public' OR tenant_id IS NULL``)
    backstops this at the storage layer.
    """
    if is_platform:
        return "public"
    raw = manifest.get("visibility")
    if raw is None:
        return "private"
    value = str(raw).strip().lower()
    if value not in _VISIBILITY_VALUES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "visibility must be one of private|protected|public.",
            details={"field": "visibility", "value": raw, "allowed": list(_VISIBILITY_VALUES)},
        )
    if value == "public":
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "A tenant tool cannot be published as 'public'; public tools are created only by "
            "platform promotion. Use 'private' or 'protected'.",
            details={"field": "visibility", "value": raw, "allowed": ["private", "protected"]},
        )
    return value


def _parse_visibility_filter(raw: str | None) -> set[str] | None:
    """Parse the optional ``?visibility=`` filter (comma-separated) into a validated set.

    Returns ``None`` when no filter was supplied (=> all visible). Rejects any token outside
    ``private|protected|public`` with a 422 VALIDATION_ERROR.
    """
    if raw is None:
        return None
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not wanted:
        return None
    invalid = wanted - set(_VISIBILITY_VALUES)
    if invalid:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "visibility filter must be a comma-separated subset of private|protected|public.",
            status_code=422,
            details={"invalid": sorted(invalid), "allowed": list(_VISIBILITY_VALUES)},
        )
    return wanted


async def _resolve_tool_view(
    pool: Any, tenant_id: str, tool_row: dict[str, Any], *, version: str | None
) -> dict[str, Any] | None:
    """Resolve one tool row into a discovery view (manifest + caps + health)."""
    tool_id = tool_row["tool_id"]
    version_row = await queries.get_version(pool, tenant_id, tool_id, version)
    if version_row is None:
        return None
    capabilities = await queries.get_capabilities(pool, tenant_id, tool_id)
    health = await queries.get_health(pool, tenant_id, tool_id)
    return discovery.build_tool_view(
        tool_row,
        manifest=version_row.get("manifest"),
        resolved_version=version_row.get("version"),
        capabilities=capabilities,
        health=health,
    )


# ── GET /v1/tools ───────────────────────────────────────────────────────────────
@router.get("/tools")
async def list_tools(
    request: Request,
    principal: Principal = Depends(require_principal),
    visibility: str | None = Query(default=None),
) -> dict[str, Any]:
    """List tools visible to the caller (platform + own tenant), tenant-priority shadowed.

    Optional ``?visibility=`` (comma-separated ``public|private|protected``) narrows the
    result to those Marketplace sections; default = all visible.
    """
    settings = request.app.state.settings
    pool = _get_pool(request)
    wanted = _parse_visibility_filter(visibility)
    # Push the visibility filter INTO the SQL so the LIMIT counts only rows of the requested
    # visibility (a post-LIMIT filter undercounts a narrowed tab when the visible set exceeds
    # the cap).
    rows = await queries.list_visible_tools(
        pool, principal.tenant_id, limit=settings.discovery_max_tools, visibility=wanted
    )
    resolved = discovery.shadow_by_tenant_priority(rows)
    # Redundant safety net: the SQL already narrowed to `wanted`, so this post-filter is a no-op
    # on the real DB — kept as a defensive backstop.
    if wanted is not None:
        resolved = [r for r in resolved if r.get("visibility") in wanted]
    data: list[dict[str, Any]] = []
    for tool_row in resolved:
        view = await _resolve_tool_view(pool, principal.tenant_id, tool_row, version=None)
        if view is not None:
            data.append(view)
    return {"data": data}


# ── GET /v1/tools/{name} ─────────────────────────────────────────────────────────
@router.get("/tools/{name}")
async def get_tool(
    request: Request,
    name: str,
    principal: Principal = Depends(require_principal),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """Resolve a single tool by name (tenant shadows platform), optionally version-pinned."""
    pool = _get_pool(request)
    rows = await queries.get_tool_rows_by_name(pool, principal.tenant_id, name)
    if not rows:
        raise ApiError(ErrorCode.NOT_FOUND, f"Tool '{name}' not found.")
    # Tenant priority: the tenant's own row shadows a platform row of the same name.
    chosen = discovery.shadow_by_tenant_priority(rows)[0]
    view = await _resolve_tool_view(pool, principal.tenant_id, chosen, version=version)
    if view is None:
        detail = f" version '{version}'" if version else ""
        raise ApiError(ErrorCode.NOT_FOUND, f"Tool '{name}'{detail} has no active version.")
    return view


# ── POST /v1/tools ───────────────────────────────────────────────────────────────
@router.post("/tools", status_code=201)
async def register_tool(
    request: Request,
    principal: Principal = Depends(require_admin),
    manifest: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Register a NEW tool (for the caller's tenant) from a Contract-4 manifest."""
    pool = _get_pool(request)
    manifest_svc.validate_manifest(manifest)
    name = manifest["name"]
    version = manifest["version"]
    capabilities = _manifest_capabilities(manifest)
    # A registration through this API always carries a tenant Principal, so is_platform is
    # False here; platform (tenant_id NULL) rows are seeded, not registered. Kept explicit so
    # the platform->public rule holds if a platform principal ever reaches this path.
    is_platform = not (principal.tenant_id or "").strip()
    visibility = _resolve_visibility(manifest, is_platform=is_platform)

    try:
        tool = await queries.create_tool_with_version(
            pool, principal.tenant_id,
            name=name, version=version, manifest=manifest, capabilities=capabilities,
            visibility=visibility,
        )
    except UniqueViolation as exc:
        raise ApiError(
            ErrorCode.CONFLICT, f"Tool '{name}' already exists for this tenant.",
        ) from exc

    from ..core import metrics

    metrics.tool_registered_total.labels("tool").inc()
    await _eager_poll(request, tool["tool_id"], manifest, name)
    return {
        "tool_id": tool["tool_id"],
        "name": name,
        "version": version,
        "owner": "tenant",
        "visibility": visibility,
        "status": "active",
    }


# ── POST /v1/tools/{name}/versions ───────────────────────────────────────────────
@router.post("/tools/{name}/versions", status_code=201)
async def register_version(
    request: Request,
    name: str,
    principal: Principal = Depends(require_admin),
    manifest: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Append a new version to an existing tenant tool; enforce active-version retention."""
    settings = request.app.state.settings
    pool = _get_pool(request)
    manifest_svc.validate_manifest(manifest)
    if manifest["name"] != name:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Manifest name does not match the path tool name.",
            details={"path": name, "manifest_name": manifest["name"]},
        )
    version = manifest["version"]

    # Resolve the tenant's OWN tool (not a platform tool) for this name.
    rows = await queries.get_tool_rows_by_name(pool, principal.tenant_id, name)
    own = next((r for r in rows if not r["is_platform"]), None)
    if own is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"Tenant tool '{name}' not found; register it first.")
    visibility = _resolve_visibility(manifest, is_platform=bool(own["is_platform"]))

    try:
        result = await queries.add_version(
            pool, principal.tenant_id,
            tool_id=own["tool_id"], version=version, manifest=manifest,
            capabilities=_manifest_capabilities(manifest),
            max_active_versions=settings.max_active_versions_per_tool,
            visibility=visibility,
        )
    except UniqueViolation as exc:
        raise ApiError(
            ErrorCode.CONFLICT, f"Version '{version}' already exists for tool '{name}'.",
        ) from exc

    from ..core import metrics

    metrics.tool_registered_total.labels("version").inc()
    for _ in result["retired"]:
        metrics.version_retired_total.inc()
    await _eager_poll(request, own["tool_id"], manifest, name)
    return {
        "tool_id": own["tool_id"],
        "name": name,
        "version": version,
        "visibility": visibility,
        "retired_versions": result["retired"],
    }


# ── Platform (public) registration (Phase 5 · 5-registry) ─────────────────────────
# The SOLE path that creates a `tenant_id NULL` / `visibility='public'` row via the API.
# A tenant registration (`POST /v1/tools`) still 400s on `public`; public == platform.
@router.post("/platform/tools", status_code=201)
async def register_platform_tool(
    request: Request,
    principal: Principal = Depends(require_platform_admin),
    manifest: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Register a NEW PLATFORM tool (``platform:admin`` only) as public.

    Runs under ``db_pool.in_platform`` (an EMPTY ``app.tenant_id`` GUC) so the shared
    ``NULLIF(current_setting('app.tenant_id', true), '')::uuid`` INSERT expression yields
    ``NULL`` — the row is stamped ``tenant_id NULL`` and admitted by the ``p_tools_platform``
    RLS policy (``tenant_id IS NULL AND empty-GUC``). ``visibility`` is forced to ``public``
    (public rows ARE the platform rows). This is the only API path that mints a public row.
    """
    pool = _get_pool(request)
    manifest_svc.validate_manifest(manifest)
    name = manifest["name"]
    version = manifest["version"]
    capabilities = _manifest_capabilities(manifest)
    # is_platform=True => forced 'public' (public rows are the platform, tenant_id NULL, rows).
    visibility = _resolve_visibility(manifest, is_platform=True)

    try:
        tool = await queries.create_tool_with_version(
            pool, principal.tenant_id,
            name=name, version=version, manifest=manifest, capabilities=capabilities,
            visibility=visibility, platform=True,
        )
    except UniqueViolation as exc:
        raise ApiError(
            ErrorCode.CONFLICT, f"Platform tool '{name}' already exists.",
        ) from exc

    from ..core import metrics

    metrics.tool_registered_total.labels("tool").inc()
    await _eager_poll(request, tool["tool_id"], manifest, name)
    return {
        "tool_id": tool["tool_id"],
        "name": name,
        "version": version,
        "owner": "platform",
        "visibility": visibility,
        "status": "active",
    }


@router.post("/platform/tools/{name}/versions", status_code=201)
async def register_platform_version(
    request: Request,
    name: str,
    principal: Principal = Depends(require_platform_admin),
    manifest: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Append a new version to an existing PLATFORM tool (``platform:admin`` only).

    Resolves the platform (``tenant_id NULL``) tool for ``name`` and versions it under
    ``db_pool.in_platform`` so the new version/capability rows stay ``tenant_id NULL`` and
    public. Mirrors ``POST /v1/tools/{name}/versions`` but on the platform namespace.
    """
    settings = request.app.state.settings
    pool = _get_pool(request)
    manifest_svc.validate_manifest(manifest)
    if manifest["name"] != name:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Manifest name does not match the path tool name.",
            details={"path": name, "manifest_name": manifest["name"]},
        )
    version = manifest["version"]

    own = await queries.get_platform_tool_by_name(pool, name)
    if own is None:
        raise ApiError(
            ErrorCode.NOT_FOUND, f"Platform tool '{name}' not found; register it first."
        )
    visibility = _resolve_visibility(manifest, is_platform=True)

    try:
        result = await queries.add_version(
            pool, principal.tenant_id,
            tool_id=own["tool_id"], version=version, manifest=manifest,
            capabilities=_manifest_capabilities(manifest),
            max_active_versions=settings.max_active_versions_per_tool,
            visibility=visibility, platform=True,
        )
    except UniqueViolation as exc:
        raise ApiError(
            ErrorCode.CONFLICT, f"Version '{version}' already exists for tool '{name}'.",
        ) from exc

    from ..core import metrics

    metrics.tool_registered_total.labels("version").inc()
    for _ in result["retired"]:
        metrics.version_retired_total.inc()
    await _eager_poll(request, own["tool_id"], manifest, name)
    return {
        "tool_id": own["tool_id"],
        "name": name,
        "version": version,
        "owner": "platform",
        "visibility": visibility,
        "retired_versions": result["retired"],
    }


# ── Retire / de-register (Phase 5 · 5-registry) ───────────────────────────────────
@router.post("/tools/{name}/retire")
async def retire_tool(
    request: Request,
    name: str,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Retire (de-register) the caller-visible tool named ``name``.

    Scope gate: a PLATFORM tool requires ``platform:admin``; a tenant tool needs the base
    admin scope (``tool:admin``/``platform:admin``) and is RLS-scoped to the caller's tenant
    (a caller can only resolve/retire its own tenant tool or a platform tool). Sets
    ``tools.status='retired'`` and retires the tool's active versions so it stops resolving in
    discovery. This backs the promote flow's de-registration of the OLD tenant ``server_name``
    after its flows are re-homed to the platform runtime.
    """
    pool = _get_pool(request)
    tool = await _resolve_own_or_platform_tool(pool, principal.tenant_id, name)
    is_platform = bool(tool.get("is_platform"))
    if is_platform and not principal.has_any_scope(("platform:admin",)):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Retiring a platform tool requires platform:admin.",
            details={"required_any": ["platform:admin"]},
        )
    await queries.set_tool_status(
        pool, principal.tenant_id, tool_id=tool["tool_id"], status="retired",
        platform=is_platform,
    )
    return {
        "tool_id": tool["tool_id"],
        "name": name,
        "status": "retired",
        "owner": "platform" if is_platform else "tenant",
    }


# ── Access control (Phase 5) ──────────────────────────────────────────────────────
async def _resolve_own_or_platform_tool(pool: Any, tenant_id: str, name: str) -> dict[str, Any]:
    rows = await queries.get_tool_rows_by_name(pool, tenant_id, name)
    if not rows:
        raise ApiError(ErrorCode.NOT_FOUND, f"Tool '{name}' not found.")
    return discovery.shadow_by_tenant_priority(rows)[0]


@router.get("/tools/{name}/access")
async def get_tool_access(
    request: Request,
    name: str,
    principal: Principal = Depends(require_principal),
    agent_id: str | None = Query(default=None),
    capability: str | None = Query(default=None),
) -> dict[str, Any]:
    """Resolve the effective access mode (none|ask|automated) for an agent + this tool server.

    Defaults to the calling agent; a tenant admin may pass ``?agent_id=`` to inspect another.
    """
    pool = _get_pool(request)
    target_agent = agent_id or principal.agent_id
    if not target_agent:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "agent_id is required.", status_code=422)
    tool = await _resolve_own_or_platform_tool(pool, principal.tenant_id, name)
    # None => not restricted (agents default to 'automated'); a string => the tool's server-wide
    # default access mode (the fallback when the agent has no explicit per-agent grant).
    restricted_default = await queries.get_restricted_default(pool, principal.tenant_id, tool["tool_id"])
    restricted = restricted_default is not None
    mode = await queries.resolve_agent_tool_access(
        pool, principal.tenant_id,
        agent_id=target_agent, tool_server_name=name, capability=capability,
        is_restricted=restricted, restricted_default=restricted_default or "none",
    )
    return {"tool": name, "agent_id": target_agent, "capability": capability,
            "access_mode": mode, "restricted": restricted}


@router.put("/tools/{name}/access")
async def set_tool_access(
    request: Request,
    name: str,
    principal: Principal = Depends(require_tenant_admin),
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Set an agent's access mode for this tool server (tenant:admin). Body:
    ``{ agent_id, access_mode, capability? }``."""
    pool = _get_pool(request)
    agent_id = str(body.get("agent_id") or "").strip()
    access_mode = str(body.get("access_mode") or "").strip()
    capability = body.get("capability")
    if not agent_id:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "agent_id is required.", status_code=422)
    if access_mode not in _ACCESS_MODES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "access_mode must be none|ask|automated.",
            status_code=422, details={"allowed": list(_ACCESS_MODES)},
        )
    # Ensure the tool exists/visible before recording access for it.
    await _resolve_own_or_platform_tool(pool, principal.tenant_id, name)
    row = await queries.set_agent_tool_access(
        pool, principal.tenant_id,
        agent_id=agent_id, tool_server_name=name,
        capability=str(capability) if capability else None, access_mode=access_mode,
    )
    return row


@router.get("/restricted-tools")
async def list_restricted(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """List tools restricted for this tenant (own + platform-wide)."""
    pool = _get_pool(request)
    return {"data": await queries.list_restricted_tools(pool, principal.tenant_id)}


@router.post("/restricted-tools/{name}", status_code=201)
async def mark_restricted(
    request: Request,
    name: str,
    principal: Principal = Depends(require_tenant_admin),
    body: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Mark a tool as restricted. Body: ``{ reason, default_access_mode? }``.

    ``default_access_mode`` (``none``|``ask``|``automated``, default ``none``) is the server-wide
    fallback an agent gets when it has no explicit per-agent grant — e.g. ``ask`` makes the tool
    callable by every tenant agent subject to HIL approval, without enumerating agents up front.
    """
    pool = _get_pool(request)
    tool = await _resolve_own_or_platform_tool(pool, principal.tenant_id, name)
    reason = str(body.get("reason") or "restricted").strip()
    default_access_mode = str(body.get("default_access_mode") or "none").strip()
    if default_access_mode not in _ACCESS_MODES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "default_access_mode must be none|ask|automated.",
            status_code=422, details={"allowed": list(_ACCESS_MODES)},
        )
    await queries.mark_tool_restricted(
        pool, principal.tenant_id, tool_id=tool["tool_id"], reason=reason,
        default_access_mode=default_access_mode,
    )
    return {"tool": name, "tool_id": tool["tool_id"], "reason": reason,
            "restricted": True, "default_access_mode": default_access_mode}


async def _eager_poll(
    request: Request, tool_id: str, manifest: dict[str, Any], name: str
) -> None:
    """Eagerly poll a freshly-registered tool's manifest so its health is known now.

    Fail-soft: any error here only logs — registration still succeeds.
    """
    client: HttpClient | None = getattr(request.app.state, "http_client", None)
    if client is None:
        return
    settings = request.app.state.settings
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return
    base_url = discovery.resolve_invoke_url(manifest, name)
    try:
        from ..services.health_runner import poll_one

        await poll_one(
            pool, client, settings,
            tool_id=str(tool_id), base_url=base_url, current=HealthState(),
        )
    except Exception as exc:  # noqa: BLE001 — eager poll is best-effort
        logger.warning("eager_poll_failed", tool_id=str(tool_id), error=str(exc))
