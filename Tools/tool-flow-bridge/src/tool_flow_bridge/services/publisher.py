"""Publish orchestration: analyze a Node-RED flow -> generate an atomic tool + its MCP ->
register it in the Tool Registry -> govern access. Holds the wiring the ``flow_tools`` +
tools/mcps APIs call; keeps the endpoints thin.

SOURCE OF TRUTH (Phase 2, finding #4): create/update writes ``flow_tools.tools`` +
``flow_tools.mcps`` + ``flow_tools.mcp_tools``. ``flow_tools.tool_bindings`` is NO LONGER
written (kept read-only for rollback/history). A standalone tool (no ``mcp_ids``) gets an
auto-created SINGLETON MCP whose ``server_name = tool-<slug>`` preserves the registry key; its
registry ``base_url`` points at the canonical ``/m/<slug>`` wire.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db import pool as db_pool
from ..db import queries
from . import manifest_builder, schema_validate
from .nodered_adapter import NoderedError, invoke_workflow
from .nodered_admin import NoderedAdmin, validate_flow_shape
from .provisioner import (
    PlatformProvisioner,
    Provisioner,
    ensure_platform_runtime,
    ensure_runtime,
    get_platform_provisioner,
)
from .registry_client import RegistryClient
from .secrets import resolve_secret

logger = structlog.get_logger(__name__)

_ACCESS_MODES = ("none", "ask", "automated")
# A tenant publish/register path may declare private|protected ONLY. `public` is reached
# solely via POST /promote (platform namespace) — the registry 400s a tenant-declared
# visibility=public, so it must never be sent on a tenant registration (finding #8 GUARD).
_TENANT_VISIBILITY = ("private", "protected")


def _bump_patch(version: str) -> str:
    try:
        major, minor, patch = (int(p) for p in version.split("."))
        return f"{major}.{minor}.{patch + 1}"
    except ValueError:
        return "1.0.1"


class Publisher:
    def __init__(
        self,
        *,
        settings: Settings,
        pool: AsyncConnectionPool,
        provisioner: Provisioner,
        registry: RegistryClient,
        nodered_admin: NoderedAdmin,
        http_client: httpx.AsyncClient,
        platform_provisioner: PlatformProvisioner | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._provisioner = provisioner
        # The SINGLETON platform (public) runtime provisioner — the promote path re-homes member
        # flows onto it. Defaults from settings so existing constructors need no change.
        self._platform_provisioner = platform_provisioner or get_platform_provisioner(settings)
        self._registry = registry
        self._admin = nodered_admin
        self._http = http_client

    # ── publish (legacy POST /v1/flow-tools — always a singleton MCP) ─────────────
    async def publish(
        self,
        principal: Principal,
        user_jwt: str,
        body: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Legacy publish wire. Delegates to the new create path (auto-singleton) and returns the
        historical response shape so the current UI/tests keep working."""
        flow_id = body.get("node_red_flow_id")
        tool = body.get("tool") or {}
        if not flow_id or not isinstance(tool, dict):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Request needs 'node_red_flow_id' and a 'tool' object.",
                status_code=422,
            )
        result = await self._create_tool(
            principal,
            user_jwt,
            flow_id=str(flow_id),
            title=str(tool.get("title", "")).strip(),
            description=str(tool.get("description", "")).strip(),
            snake_name_hint=tool.get("snake_name"),
            input_params=tool.get("input_params"),
            output_params=tool.get("output_params"),
            access_mode=str(tool.get("access_mode") or self._settings.default_access_mode).lower(),
            visibility=str(tool.get("visibility") or "private").lower(),
            mcp_ids=None,
            trace_headers=trace_headers,
        )
        slug = result["mcp_slug"]
        return {
            "slug": slug,
            "server_name": result["server_name"],
            "tool_name": result["snake_name"],
            "version": result["version"],
            "invoke_url": f"{self._settings.bridge_base_url.rstrip('/')}/m/{slug}",
            "access_mode": result["access_mode"],
            "is_update": result["is_update"],
        }

    # ── create an atomic tool (POST /v1/tools) ────────────────────────────────────
    async def create_tool(
        self,
        principal: Principal,
        user_jwt: str,
        body: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create an atomic tool from a Node-RED flow. With ``mcp_ids`` the tool joins those MCPs;
        without, it gets an auto-created singleton MCP (``server_name = tool-<slug>``)."""
        flow_id = body.get("node_red_flow_id")
        if not flow_id:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "'node_red_flow_id' is required.", status_code=422)
        raw_mcp_ids = body.get("mcp_ids")
        mcp_ids: list[str] | None = None
        if raw_mcp_ids is not None:
            if not isinstance(raw_mcp_ids, list):
                raise ApiError(ErrorCode.VALIDATION_ERROR, "'mcp_ids' must be an array.", status_code=422)
            mcp_ids = [str(m) for m in raw_mcp_ids] or None
        return await self._create_tool(
            principal,
            user_jwt,
            flow_id=str(flow_id),
            title=str(body.get("title", "")).strip(),
            description=str(body.get("description", "")).strip(),
            snake_name_hint=body.get("snake_name"),
            input_params=body.get("input_params"),
            output_params=body.get("output_params"),
            access_mode=str(body.get("access_mode") or self._settings.default_access_mode).lower(),
            visibility=str(body.get("visibility") or "private").lower(),
            mcp_ids=mcp_ids,
            trace_headers=trace_headers,
        )

    async def _create_tool(
        self,
        principal: Principal,
        user_jwt: str,
        *,
        flow_id: str,
        title: str,
        description: str,
        snake_name_hint: Any,
        input_params: Any,
        output_params: Any,
        access_mode: str,
        visibility: str,
        mcp_ids: list[str] | None,
        trace_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if not principal.agent_id:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Publishing requires an agent identity (agent_id) on the token.",
                status_code=422,
            )
        if not title or not description:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, "Tool 'title' and 'description' are required.",
                status_code=422,
            )
        if access_mode not in _ACCESS_MODES:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, f"access_mode must be one of {_ACCESS_MODES}.",
                status_code=422,
            )
        if visibility not in _TENANT_VISIBILITY:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "visibility must be 'private' or 'protected'. 'public' is reached only via promote.",
                status_code=422,
            )

        # 1. Ensure the tenant's Node-RED runtime exists.
        runtime = await ensure_runtime(
            self._pool, principal.tenant_id, self._provisioner, self._settings
        )
        admin_token = resolve_secret(runtime["admin_token_ref"], self._settings)

        # 2. Read + validate the flow shape (http in -> http response).
        flow = await self._admin.get_flow(
            internal_host=runtime["internal_host"], admin_token=admin_token, flow_id=str(flow_id)
        )
        shape = validate_flow_shape(flow)

        # 2b. Best-effort (re)deploy so the http-in route is live before agents can invoke.
        await self._admin.redeploy_flow(
            internal_host=runtime["internal_host"], admin_token=admin_token,
            flow_id=str(flow_id), flow=flow,
        )

        # 3. Names + schemas.
        snake_name, slug, _tool_server = manifest_builder.build_names(
            principal.tenant_id, title, snake_name_hint
        )
        input_schema = manifest_builder.form_to_input_schema(input_params)
        output_schema = manifest_builder.form_to_output_schema(output_params)
        singleton_slug, singleton_server = manifest_builder.singleton_mcp_names(slug)
        use_singleton = not mcp_ids

        # 4. Write the new model (tool + membership) atomically; capture rollback snapshots.
        async def _write(conn: Any) -> dict[str, Any]:
            prior_tool = await queries.get_tool_by_snake_name(conn, snake_name)
            is_update = prior_tool is not None
            if prior_tool is not None:
                version = _bump_patch(prior_tool["version"])
                tool_row = await queries.update_tool(
                    conn, prior_tool["tool_id"],
                    snake_name=snake_name, display_name=title, description=description,
                    input_schema=input_schema, output_schema=output_schema,
                    node_red_flow_id=str(flow_id), http_method=shape.http_method,
                    http_path=shape.http_path, version=version, access_mode=access_mode,
                    visibility=visibility,
                )
            else:
                version = "1.0.0"
                tool_row = await queries.create_tool(
                    conn, principal.tenant_id,
                    snake_name=snake_name, display_name=title, description=description,
                    input_schema=input_schema, output_schema=output_schema,
                    node_red_flow_id=str(flow_id), http_method=shape.http_method,
                    http_path=shape.http_path, runtime_id=runtime["runtime_id"],
                    version=version, access_mode=access_mode, visibility=visibility,
                )
            tool_id = str(tool_row["tool_id"])

            affected: list[dict[str, Any]] = []
            prior_mcp: dict[str, Any] | None = None
            if use_singleton:
                prior_mcp = await queries.get_mcp_by_slug(conn, singleton_slug)
                if prior_mcp is not None:
                    mcp_row = await queries.update_mcp(
                        conn, prior_mcp["mcp_id"], display_name=title, description=description,
                        visibility=visibility, version=version,
                    )
                else:
                    mcp_row = await queries.create_mcp(
                        conn, principal.tenant_id, slug=singleton_slug, server_name=singleton_server,
                        display_name=title, description=description, visibility=visibility,
                        version=version,
                    )
                await queries.add_mcp_member(conn, str(mcp_row["mcp_id"]), tool_id, principal.tenant_id)
                affected.append(mcp_row)
            else:
                # Ownership-validate the caller-supplied MCPs (RLS + app layer), then attach.
                assert mcp_ids is not None
                for mid in mcp_ids:
                    target = await queries.get_mcp_by_id(conn, mid)
                    if target is None:
                        raise ApiError(
                            ErrorCode.FORBIDDEN,
                            f"MCP '{mid}' is not owned by this tenant.",
                        )
                    await queries.add_mcp_member(conn, mid, tool_id, principal.tenant_id)
                    affected.append(target)

            return {
                "tool": tool_row, "affected": affected, "is_update": is_update,
                "version": version, "prior_tool": prior_tool, "prior_mcp": prior_mcp,
            }

        written = await db_pool.in_tenant(self._pool, principal.tenant_id, _write)
        tool_row = written["tool"]
        affected = written["affected"]
        is_update = bool(written["is_update"])
        version = str(written["version"])

        # 5. Register each affected MCP in the registry (rollback the new-model write on failure).
        try:
            for mcp_row in affected:
                await self._register_mcp(
                    user_jwt, principal.agent_id, mcp_row, is_update=is_update,
                    trace_headers=trace_headers,
                )
        except ApiError:
            await self._rollback_create(principal, written, use_singleton=use_singleton)
            metrics.publish_total.labels("publish", "error").inc()
            raise

        # 6. Access posture — only the auto-singleton carries the tool's per-tool default onto its
        #    (1:1) server. A shared MCP's access is governed per-agent, so we don't coarsely
        #    restrict it from one member's mode.
        if use_singleton and access_mode != "automated":
            await self._registry.mark_restricted(
                user_jwt=user_jwt, agent_id=principal.agent_id, name=singleton_server,
                reason=f"flow-tool default access '{access_mode}' (publisher-selected)",
                default_access_mode=access_mode, trace_headers=trace_headers,
            )

        metrics.publish_total.labels("publish", "ok").inc()
        metrics.publish_duration_seconds.observe(time.monotonic() - started)
        logger.info("tool_published", slug=slug, version=version, is_update=is_update)
        memberships = [
            {"mcp_id": str(m["mcp_id"]), "slug": m["slug"], "server_name": m["server_name"]}
            for m in affected
        ]
        return {
            "tool_id": str(tool_row["tool_id"]),
            "snake_name": snake_name,
            "display_name": title,
            "description": description,
            "version": version,
            "visibility": visibility,
            "access_mode": access_mode,
            "status": "active",
            "is_update": is_update,
            "mcp_slug": singleton_slug if use_singleton else (affected[0]["slug"] if affected else None),
            "server_name": singleton_server if use_singleton else (
                affected[0]["server_name"] if affected else None
            ),
            "mcps": memberships,
        }

    async def _rollback_create(
        self, principal: Principal, written: dict[str, Any], *, use_singleton: bool
    ) -> None:
        """Undo a new-model write whose registry registration failed, so ``/m`` never advertises a
        version the registry rejected. On a fresh create -> retire the new tool (+ singleton); on a
        re-publish -> restore the prior tool + singleton MCP fields exactly."""
        tool_row = written["tool"]
        prior_tool = written["prior_tool"]
        prior_mcp = written["prior_mcp"]
        affected = written["affected"]

        async def _do(conn: Any) -> None:
            if prior_tool is not None:
                await queries.update_tool(
                    conn, prior_tool["tool_id"],
                    snake_name=prior_tool["snake_name"], display_name=prior_tool["display_name"],
                    description=prior_tool["description"], input_schema=prior_tool["input_schema"],
                    output_schema=prior_tool.get("output_schema"),
                    node_red_flow_id=prior_tool["node_red_flow_id"],
                    http_method=prior_tool["http_method"], http_path=prior_tool["http_path"],
                    version=prior_tool["version"], access_mode=prior_tool["access_mode"],
                    visibility=prior_tool["visibility"],
                )
            else:
                await queries.set_tool_status(conn, str(tool_row["tool_id"]), "retired")
            if use_singleton and affected:
                mcp_id = str(affected[0]["mcp_id"])
                if prior_mcp is not None:
                    await queries.update_mcp(
                        conn, prior_mcp["mcp_id"], display_name=prior_mcp["display_name"],
                        description=prior_mcp["description"], visibility=prior_mcp["visibility"],
                        version=prior_mcp["version"],
                    )
                else:
                    await queries.set_mcp_status(conn, mcp_id, "retired")

        await db_pool.in_tenant(self._pool, principal.tenant_id, _do)

    async def _register_mcp(
        self,
        user_jwt: str,
        agent_id: str,
        mcp_row: dict[str, Any],
        *,
        is_update: bool,
        trace_headers: dict[str, str] | None,
        members: list[dict[str, Any]] | None = None,
    ) -> None:
        """(Re)generate the aggregating manifest for ``mcp_row`` and register/refresh it in the
        registry under ``mcp_row['server_name']`` (base_url = the canonical ``/m/<slug>`` wire)."""
        if members is None:
            async def _load(conn: Any) -> list[dict[str, Any]]:
                return await queries.get_mcp_members(conn, str(mcp_row["mcp_id"]))

            members = await db_pool.in_tenant(self._pool, mcp_row["tenant_id"], _load)
        manifest = manifest_builder.build_mcp_manifest(
            self._settings, mcp=mcp_row, member_tools=members
        )
        await self._registry.register(
            user_jwt=user_jwt, agent_id=agent_id, name=mcp_row["server_name"],
            manifest=manifest, is_update=is_update, trace_headers=trace_headers,
        )

    # ── MCP collections (POST/GET/PUT/DELETE /v1/mcps) ────────────────────────────
    async def create_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        body: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create an MCP collection from ``display_name``, ``description``, ``visibility`` and
        ``tool_ids[]`` (every tool_id ownership-validated), then register the aggregating server."""
        if not principal.agent_id:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "An agent identity is required.", status_code=422)
        display_name = str(body.get("display_name") or body.get("title") or "").strip()
        description = str(body.get("description") or "").strip()
        if not display_name or not description:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, "MCP 'display_name' and 'description' are required.",
                status_code=422,
            )
        visibility = str(body.get("visibility") or "private").lower()
        if visibility not in _TENANT_VISIBILITY:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "visibility must be 'private' or 'protected'. 'public' is reached only via promote.",
                status_code=422,
            )
        raw_ids = body.get("tool_ids") or []
        if not isinstance(raw_ids, list):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "'tool_ids' must be an array.", status_code=422)
        tool_ids = [str(t) for t in raw_ids]
        slug, server_name = manifest_builder.build_mcp_names(
            principal.tenant_id, display_name, body.get("slug")
        )

        async def _write(conn: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            await self._assert_tools_owned(conn, tool_ids)
            mcp_row = await queries.create_mcp(
                conn, principal.tenant_id, slug=slug, server_name=server_name,
                display_name=display_name, description=description, visibility=visibility,
                version="1.0.0",
            )
            await queries.set_mcp_members(conn, str(mcp_row["mcp_id"]), principal.tenant_id, tool_ids)
            members = await queries.get_mcp_members(conn, str(mcp_row["mcp_id"]))
            return mcp_row, members

        mcp_row, members = await db_pool.in_tenant(self._pool, principal.tenant_id, _write)
        try:
            await self._register_mcp(
                user_jwt, principal.agent_id, mcp_row, is_update=False,
                trace_headers=trace_headers, members=members,
            )
        except ApiError:
            async def _retire(conn: Any) -> None:
                await queries.set_mcp_status(conn, str(mcp_row["mcp_id"]), "retired")

            await db_pool.in_tenant(self._pool, principal.tenant_id, _retire)
            raise
        metrics.publish_total.labels("publish", "ok").inc()
        return _mcp_view(mcp_row, members)

    async def update_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        mcp_id: str,
        body: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Update an MCP's metadata/membership (ownership-validated) and re-register its manifest.
        The version is STABLE (auto-refresh projection): a metadata/membership change does not churn
        the tool version — the registry picks up the regenerated manifest via its ETag poll."""
        if not principal.agent_id:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "An agent identity is required.", status_code=422)

        async def _write(conn: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            mcp = await queries.get_mcp_by_id(conn, mcp_id)
            if mcp is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"No MCP with id '{mcp_id}'.")
            display_name = str(body.get("display_name") or mcp["display_name"]).strip()
            description = str(body.get("description") or mcp["description"]).strip()
            visibility = str(body.get("visibility") or mcp["visibility"]).lower()
            if visibility not in _TENANT_VISIBILITY:
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR,
                    "visibility must be 'private' or 'protected'.", status_code=422,
                )
            if "tool_ids" in body:
                raw = body.get("tool_ids") or []
                if not isinstance(raw, list):
                    raise ApiError(
                        ErrorCode.VALIDATION_ERROR, "'tool_ids' must be an array.", status_code=422
                    )
                tool_ids = [str(t) for t in raw]
                await self._assert_tools_owned(conn, tool_ids)
                await queries.set_mcp_members(conn, mcp_id, principal.tenant_id, tool_ids)
            row = await queries.update_mcp(
                conn, mcp_id, display_name=display_name, description=description,
                visibility=visibility, version=mcp["version"],  # STABLE
            )
            members = await queries.get_mcp_members(conn, mcp_id)
            return row, members

        mcp_row, members = await db_pool.in_tenant(self._pool, principal.tenant_id, _write)
        await self._register_mcp(
            user_jwt, principal.agent_id, mcp_row, is_update=True,
            trace_headers=trace_headers, members=members,
        )
        return _mcp_view(mcp_row, members)

    async def publish_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        mcp_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """(Re)register/refresh an MCP in the registry (POST /v1/mcps/{id}/publish)."""
        if not principal.agent_id:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "An agent identity is required.", status_code=422)

        async def _load(conn: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            mcp = await queries.get_mcp_by_id(conn, mcp_id)
            if mcp is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"No MCP with id '{mcp_id}'.")
            members = await queries.get_mcp_members(conn, mcp_id)
            return mcp, members

        mcp_row, members = await db_pool.in_tenant(self._pool, principal.tenant_id, _load)
        await self._register_mcp(
            user_jwt, principal.agent_id, mcp_row, is_update=True,
            trace_headers=trace_headers, members=members,
        )
        return _mcp_view(mcp_row, members)

    async def unpublish_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        mcp_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """DELETE /v1/mcps/{id} — retire the MCP AND its exclusively-owned tools (finding #4 case c),
        so a retired tool stops being resolvable/invokable via ``/m``."""
        async def _load(conn: Any) -> dict[str, Any] | None:
            return await queries.get_mcp_by_id(conn, mcp_id)

        mcp = await db_pool.in_tenant(self._pool, principal.tenant_id, _load)
        if mcp is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No MCP with id '{mcp_id}'.")
        return await self._retire_mcp(principal, user_jwt, mcp, trace_headers=trace_headers)

    async def promote_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        mcp_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/mcps/{id}/promote (platform:admin) — the SOLE path to Public.

        Ensures the singleton platform (public) Node-RED runtime, RE-HOMES the MCP's member flows
        onto it, registers the MCP under the PLATFORM namespace (``mcp-<snakeslug>``,
        visibility=public, author=platform → a registry ``tenant_id NULL`` public row), then
        de-registers the OLD tenant ``server_name`` and marks ``runtime_rehomed=True``.

        ORDERING / no-brick guarantee: the member flows are COPIED into the platform runtime
        (non-destructive — the tenant flows remain) and the platform tool is REGISTERED before ANY
        destructive local change. Only after the platform registration succeeds is the local rename
        + runtime repoint COMMITTED. A failure at any step therefore leaves the source MCP private +
        invokable (its ``/m/<old-slug>`` wire, tenant runtime, and tenant registry entry intact); any
        additive platform-side effects (copied flows, a registration) are rolled back best-effort."""
        if not principal.agent_id:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "An agent identity is required.", status_code=422)

        # 1) READ ONLY: load the MCP + members. No mutation yet.
        async def _load(conn: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            mcp = await queries.get_mcp_by_id(conn, mcp_id)
            if mcp is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"No MCP with id '{mcp_id}'.")
            members = await queries.get_mcp_members(conn, mcp_id)
            return mcp, members

        mcp, members = await db_pool.in_tenant(self._pool, principal.tenant_id, _load)
        old_server_name = str(mcp["server_name"])
        platform_slug, platform_server = manifest_builder.platform_mcp_names(
            mcp["slug"], str(mcp["tenant_id"])
        )
        promoted_version = _bump_patch(mcp["version"])

        # 2) Ensure BOTH the source tenant runtime (to read the member flows) and the SINGLETON
        #    platform runtime (egress-ALLOW; the re-home target). No DB mutation of the MCP yet.
        source_runtime = await ensure_runtime(
            self._pool, principal.tenant_id, self._provisioner, self._settings
        )
        source_admin_token = resolve_secret(source_runtime["admin_token_ref"], self._settings)
        platform_runtime = await ensure_platform_runtime(
            self._pool, self._platform_provisioner, self._settings
        )
        platform_admin_token = resolve_secret(platform_runtime["admin_token_ref"], self._settings)
        platform_runtime_id = str(platform_runtime["runtime_id"])

        # 3) RE-HOME (copy): pull each member's Node-RED flow from the tenant runtime and create it
        #    on the platform runtime. NON-destructive (the tenant flows remain). Collect the new
        #    platform flow ids so the tool rows can be repointed once the platform registration
        #    commits. A failure here rolls back the created flows and leaves the MCP private.
        rehomed: list[tuple[str, str]] = []  # (tool_id, new_platform_flow_id)
        created_flow_ids: list[str] = []
        try:
            for m in members:
                flow = await self._admin.get_flow(
                    internal_host=source_runtime["internal_host"],
                    admin_token=source_admin_token,
                    flow_id=str(m["node_red_flow_id"]),
                )
                new_flow_id = await self._admin.create_flow(
                    internal_host=platform_runtime["internal_host"],
                    admin_token=platform_admin_token,
                    flow=flow,
                )
                created_flow_ids.append(new_flow_id)
                rehomed.append((str(m["tool_id"]), new_flow_id))
        except ApiError as exc:
            await self._delete_platform_flows(platform_runtime, platform_admin_token, created_flow_ids)
            metrics.publish_total.labels("promote", "error").inc()
            logger.warning("promote_rehome_failed", mcp_id=mcp_id, error=exc.message)
            raise

        # 4) Build the PLATFORM manifest projection (author=platform, visibility=public, tenant_id
        #    NULL) and REGISTER FIRST via the platform path. No destructive local change until this
        #    succeeds — a rejection undoes the additive flow copies and leaves the MCP private.
        platform_mcp = {
            **mcp, "slug": platform_slug, "server_name": platform_server,
            "version": promoted_version, "visibility": "public", "tenant_id": None,
        }
        manifest = manifest_builder.build_mcp_manifest(
            self._settings, mcp=platform_mcp, member_tools=members
        )
        try:
            await self._registry.register_platform(
                user_jwt=user_jwt, agent_id=principal.agent_id, name=str(platform_server),
                manifest=manifest, trace_headers=trace_headers,
            )
        except ApiError as exc:
            await self._delete_platform_flows(platform_runtime, platform_admin_token, created_flow_ids)
            metrics.publish_total.labels("promote", "error").inc()
            logger.warning("promote_registry_rejected", mcp_id=mcp_id, error=exc.message)
            raise ApiError(
                exc.code,
                f"Cannot promote MCP '{mcp['slug']}' to public: {exc.message}",
                status_code=exc.status_code,
            ) from exc

        # 5) Registry accepted — COMMIT the local platform rename + runtime repoint ATOMICALLY.
        #    ALSO flip each member tool to visibility='public' in the SAME txn: the mcps row going
        #    public is not enough — a FOREIGN tenant's /m/<slug> resolve reads the member tools too,
        #    and the cross-tenant read is gated by the tools _public_read RLS policy (migration 0007,
        #    USING visibility='public'). Without this the public MCP resolves with ZERO members in a
        #    foreign context. Committed together so the row set is never half-public.
        rehomed_tool_ids = [tool_id for tool_id, _ in rehomed]

        async def _commit(conn: Any) -> dict[str, Any]:
            try:
                row = await queries.promote_mcp_row(
                    conn, mcp_id, slug=platform_slug, server_name=platform_server,
                    visibility="public", version=promoted_version,
                )
            except UniqueViolation as exc:  # two tenants promoting same-named MCPs -> typed 409
                raise ApiError(
                    ErrorCode.CONFLICT,
                    f"A public MCP named '{platform_slug}' already exists.",
                    status_code=409,
                ) from exc
            for tool_id, new_flow_id in rehomed:
                await queries.repoint_tool_runtime(
                    conn, tool_id, runtime_id=platform_runtime_id, node_red_flow_id=new_flow_id
                )
            # Member tools become public so the cross-tenant _public_read policy admits them.
            # KNOWN LIMITATION: if a member tool is ALSO in a PRIVATE MCP, this makes that shared
            # tool row public (and repoints its runtime above), widening its read exposure — promote
            # is intended for DEDICATED MCPs whose members are not shared with a private MCP.
            await queries.set_tools_visibility(conn, rehomed_tool_ids, "public")
            return row

        try:
            mcp_row = await db_pool.in_tenant(self._pool, principal.tenant_id, _commit)
        except ApiError:
            # Local commit failed AFTER the platform registration (e.g. a name collision) — undo
            # the platform-side effects so nothing is orphaned, then surface the error.
            await self._delete_platform_flows(platform_runtime, platform_admin_token, created_flow_ids)
            try:
                await self._registry.retire(
                    user_jwt=user_jwt, agent_id=principal.agent_id, name=str(platform_server),
                    trace_headers=trace_headers,
                )
            except ApiError as exc:
                logger.warning(
                    "promote_rollback_retire_failed", server_name=platform_server, error=exc.message
                )
            metrics.publish_total.labels("promote", "error").inc()
            raise

        # 6) De-register the OLD tenant server_name (best-effort) now that Public is live, so the
        #    stale /m/<old-slug> registry entry is not orphaned.
        try:
            await self._registry.retire(
                user_jwt=user_jwt, agent_id=principal.agent_id, name=old_server_name,
                trace_headers=trace_headers,
            )
        except ApiError as exc:
            logger.warning("promote_retire_old_failed", server_name=old_server_name, error=exc.message)

        metrics.publish_total.labels("promote", "ok").inc()
        logger.info(
            "mcp_promoted", mcp_id=mcp_id, slug=platform_slug, server_name=platform_server,
            runtime_rehomed=True,
        )
        view = _mcp_view(mcp_row, members)
        view["registry_status"] = "registered"
        view["runtime_rehomed"] = True
        return view

    async def _delete_platform_flows(
        self, platform_runtime: dict[str, Any], admin_token: str, flow_ids: list[str]
    ) -> None:
        """Best-effort rollback: delete flows copied into the platform runtime when a later promote
        step fails. Never raises (``delete_flow`` swallows its own errors)."""
        for fid in flow_ids:
            await self._admin.delete_flow(
                internal_host=platform_runtime["internal_host"], admin_token=admin_token, flow_id=fid
            )

    async def _retire_mcp(
        self,
        principal: Principal,
        user_jwt: str,
        mcp: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        mcp_id = str(mcp["mcp_id"])

        async def _retire(conn: Any) -> list[str]:
            exclusive = await queries.exclusive_member_tool_ids(conn, mcp_id)
            await queries.set_mcp_status(conn, mcp_id, "retired")
            for tid in exclusive:
                await queries.set_tool_status(conn, tid, "retired")
            return exclusive

        retired_tools = await db_pool.in_tenant(self._pool, principal.tenant_id, _retire)

        # Best-effort: the registry has no hard delete — mark the server restricted so agents can't
        # call it. The status flips make /m + /manifest return 404 (registry health -> offline).
        if principal.agent_id:
            try:
                await self._registry.mark_restricted(
                    user_jwt=user_jwt, agent_id=principal.agent_id, name=mcp["server_name"],
                    reason="MCP unpublished", trace_headers=trace_headers,
                )
            except ApiError as exc:
                logger.warning("unpublish_restrict_failed", mcp_id=mcp_id, error=exc.message)

        metrics.publish_total.labels("unpublish", "ok").inc()
        return {
            "mcp_id": mcp_id, "slug": mcp["slug"], "status": "retired",
            "retired_tools": retired_tools,
        }

    async def _assert_tools_owned(self, conn: Any, tool_ids: list[str]) -> None:
        """Reject any tool_id not owned by the current tenant (finding #3 app-layer check). The
        strengthened mcp_tools WITH CHECK (migration 0005) is the storage backstop."""
        if not tool_ids:
            return
        owned = await queries.owned_tool_ids(conn, tool_ids)
        missing = [t for t in tool_ids if t not in owned]
        if missing:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                f"tool_ids not owned by this tenant: {missing}.",
                details={"unauthorized_tool_ids": missing},
            )

    async def list_mcps(self, principal: Principal) -> list[dict[str, Any]]:
        async def _load(conn: Any) -> list[dict[str, Any]]:
            mcps = await queries.list_mcps(conn)
            out: list[dict[str, Any]] = []
            for m in mcps:
                members = await queries.get_mcp_members(conn, str(m["mcp_id"]))
                out.append(_mcp_view(m, members))
            return out

        return await db_pool.in_tenant(self._pool, principal.tenant_id, _load)

    # ── test a published tool (run it with sample args, owner-only) ──────────
    async def test_tool(
        self, principal: Principal, slug: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        async def _get(conn: Any) -> Any:
            return await queries.get_mcp_with_members(conn, slug)

        loaded = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if loaded is None or loaded[0]["status"] != "active" or not loaded[1]:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")
        tool = loaded[1][0]

        try:
            schema_validate.validate(args, tool["input_schema"])
        except schema_validate.SchemaViolation as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Input schema validation failed: {exc.message}",
                status_code=422,
                details={"pointer": exc.pointer, "reason": exc.message},
            ) from exc

        secret = resolve_secret(tool["invoke_secret_ref"], self._settings)
        try:
            result = await invoke_workflow(
                self._http,
                internal_host=tool["internal_host"],
                http_node_root=tool["http_node_root"],
                http_path=tool["http_path"],
                method=tool["http_method"],
                args=args,
                secret=secret,
                secret_header=self._settings.nodered_invoke_secret_header,
                timeout=self._settings.nodered_invoke_timeout_seconds,
            )
        except NoderedError as exc:
            status = 502 if exc.retryable else 422
            code = ErrorCode.SERVICE_UNAVAILABLE if exc.retryable else ErrorCode.VALIDATION_ERROR
            raise ApiError(code, exc.message, status_code=status) from exc
        return {"tool": tool["snake_name"], "result": result}

    # ── deploy a flow definition into a runtime (bootstrap seam) ──────────────
    async def deploy_flow(
        self, principal: Principal, flow: dict[str, Any], *, platform: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """Deploy a flow DEFINITION into the tenant (or platform) Node-RED runtime and return its
        freshly-assigned ``node_red_flow_id`` + the runtime row. Ensures the runtime first, resolves
        its admin token, then POSTs the flow via the Admin API (``create_flow`` strips the source id
        and returns the new one). Used by the ``web_search`` public-tool bootstrap to install the
        packaged flow before publishing it; reuses the same ``ensure_runtime`` / ``create_flow`` paths
        the publish + promote flows use."""
        if platform:
            runtime = await ensure_platform_runtime(
                self._pool, self._platform_provisioner, self._settings
            )
        else:
            runtime = await ensure_runtime(
                self._pool, principal.tenant_id, self._provisioner, self._settings
            )
        admin_token = resolve_secret(runtime["admin_token_ref"], self._settings)
        flow_id = await self._admin.create_flow(
            internal_host=runtime["internal_host"], admin_token=admin_token, flow=flow
        )
        return flow_id, runtime

    # ── list Node-RED flows (publish picker) ─────────────────────────────────
    async def list_flows(self, principal: Principal) -> list[dict[str, str]]:
        runtime = await ensure_runtime(
            self._pool, principal.tenant_id, self._provisioner, self._settings
        )
        admin_token = resolve_secret(runtime["admin_token_ref"], self._settings)
        return await self._admin.list_flow_tabs(
            internal_host=runtime["internal_host"], admin_token=admin_token
        )

    # ── list / get / unpublish (atomic tools; legacy /v1/flow-tools compatible) ──
    async def list_tools(self, principal: Principal) -> list[dict[str, Any]]:
        async def _load(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return await queries.list_tools(conn), await queries.list_tool_memberships(conn)

        tools, memberships = await db_pool.in_tenant(self._pool, principal.tenant_id, _load)
        by_tool: dict[str, list[dict[str, Any]]] = {}
        for m in memberships:
            by_tool.setdefault(str(m["tool_id"]), []).append(
                {
                    "mcp_id": str(m["mcp_id"]), "slug": m["mcp_slug"],
                    "server_name": m["mcp_server_name"], "status": m["mcp_status"],
                }
            )
        return [_tool_view(t, by_tool.get(str(t["tool_id"]), [])) for t in tools]

    async def get_tool(self, principal: Principal, slug: str) -> dict[str, Any]:
        async def _get(conn: Any) -> Any:
            return await queries.get_mcp_with_members(conn, slug)

        loaded = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if loaded is None or not loaded[1]:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")
        mcp, members = loaded
        membership = [{"mcp_id": str(mcp["mcp_id"]), "slug": mcp["slug"],
                       "server_name": mcp["server_name"], "status": mcp["status"]}]
        return _tool_view(members[0], membership)

    async def unpublish(
        self,
        principal: Principal,
        user_jwt: str,
        slug: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Legacy DELETE /v1/flow-tools/{slug} — retire the tool's singleton MCP (+ the tool)."""
        async def _get(conn: Any) -> dict[str, Any] | None:
            return await queries.get_mcp_by_slug(conn, slug)

        mcp = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if mcp is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")
        result = await self._retire_mcp(principal, user_jwt, mcp, trace_headers=trace_headers)
        return {"slug": slug, "status": "retired", "retired_tools": result["retired_tools"]}


def _tool_view(row: dict[str, Any], memberships: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape an atomic-tool row + its MCP memberships for the frontend (no secret refs/hosts)."""
    return {
        "tool_id": str(row["tool_id"]),
        "snake_name": row["snake_name"],
        "display_name": row["display_name"],
        "description": row["description"],
        "version": row["version"],
        "visibility": row.get("visibility"),
        "access_mode": row.get("access_mode"),
        "status": row["status"],
        "node_red_flow_id": row.get("node_red_flow_id"),
        "input_schema": row["input_schema"],
        "output_schema": row.get("output_schema"),
        "mcps": memberships,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _mcp_view(row: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape an MCP row + its member tools for the frontend."""
    return {
        "mcp_id": str(row["mcp_id"]),
        "slug": row["slug"],
        "server_name": row["server_name"],
        "display_name": row["display_name"],
        "description": row["description"],
        "visibility": row["visibility"],
        "status": row["status"],
        "version": row["version"],
        "tools": [
            {"tool_id": str(t["tool_id"]), "snake_name": t["snake_name"],
             "display_name": t["display_name"], "access_mode": t.get("access_mode")}
            for t in members
        ],
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }
