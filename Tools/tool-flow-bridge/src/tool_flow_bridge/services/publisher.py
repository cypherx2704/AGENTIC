"""Publish orchestration: analyze a Node-RED flow -> generate a Contract-4 tool ->
register it in the Tool Registry -> govern access. Holds the wiring the ``flow_tools`` API
calls; keeps the endpoints thin.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
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
from .provisioner import Provisioner, ensure_runtime
from .registry_client import RegistryClient
from .secrets import resolve_secret

logger = structlog.get_logger(__name__)

_ACCESS_MODES = ("none", "ask", "automated")


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
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._provisioner = provisioner
        self._registry = registry
        self._admin = nodered_admin
        self._http = http_client

    # ── publish ──────────────────────────────────────────────────────────────
    async def publish(
        self,
        principal: Principal,
        user_jwt: str,
        body: dict[str, Any],
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if not principal.agent_id:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Publishing requires an agent identity (agent_id) on the token.",
                status_code=422,
            )
        flow_id = body.get("node_red_flow_id")
        tool = body.get("tool") or {}
        if not flow_id or not isinstance(tool, dict):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Request needs 'node_red_flow_id' and a 'tool' object.",
                status_code=422,
            )
        title = str(tool.get("title", "")).strip()
        description = str(tool.get("description", "")).strip()
        if not title or not description:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, "Tool 'title' and 'description' are required.",
                status_code=422,
            )
        access_mode = str(tool.get("access_mode") or self._settings.default_access_mode).lower()
        if access_mode not in _ACCESS_MODES:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, f"access_mode must be one of {_ACCESS_MODES}.",
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

        # 3. Names + schemas.
        snake_name, slug, server_name = manifest_builder.build_names(
            principal.tenant_id, title, tool.get("snake_name")
        )
        input_schema = manifest_builder.form_to_input_schema(tool.get("input_params"))
        output_schema = manifest_builder.form_to_output_schema(tool.get("output_params"))

        # 4. Create vs update (version) — detect an existing binding for this slug.
        async def _existing(conn):
            return await queries.get_binding_by_slug(conn, slug)

        existing = await db_pool.in_tenant(self._pool, principal.tenant_id, _existing)
        is_update = existing is not None
        version = _bump_patch(existing["version"]) if is_update else "1.0.0"

        manifest = manifest_builder.build_manifest(
            self._settings,
            slug=slug,
            snake_name=snake_name,
            display_name=title,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            version=version,
            tenant_id=principal.tenant_id,
        )

        # 5. Upsert the binding FIRST so the registry's eager /manifest poll succeeds.
        async def _write(conn):
            if is_update:
                return await queries.update_binding(
                    conn,
                    existing["binding_id"],
                    snake_name=snake_name,
                    display_name=title,
                    description=description,
                    node_red_flow_id=str(flow_id),
                    http_method=shape.http_method,
                    http_path=shape.http_path,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    manifest=manifest,
                    version=version,
                    access_mode=access_mode,
                )
            return await queries.create_binding(
                conn,
                principal.tenant_id,
                slug=slug,
                snake_name=snake_name,
                display_name=title,
                description=description,
                runtime_id=runtime["runtime_id"],
                node_red_flow_id=str(flow_id),
                http_method=shape.http_method,
                http_path=shape.http_path,
                input_schema=input_schema,
                output_schema=output_schema,
                manifest=manifest,
                version=version,
                access_mode=access_mode,
            )

        binding = await db_pool.in_tenant(self._pool, principal.tenant_id, _write)

        # 6. Register in the Tool Registry (rollback the fresh binding on failure).
        try:
            await self._registry.register(
                user_jwt=user_jwt,
                agent_id=principal.agent_id,
                name=server_name,
                manifest=manifest,
                is_update=is_update,
                trace_headers=trace_headers,
            )
        except ApiError:
            if not is_update:
                async def _retire(conn):
                    await queries.set_binding_status(conn, binding["binding_id"], "retired")

                await db_pool.in_tenant(self._pool, principal.tenant_id, _retire)
            metrics.publish_total.labels("publish", "error").inc()
            raise

        # 7. Access posture. 'automated' => leave unrestricted; 'ask'/'none' => restrict
        #    (default-deny) so no agent can call it until a tenant admin grants access.
        if access_mode != "automated":
            await self._registry.mark_restricted(
                user_jwt=user_jwt,
                agent_id=principal.agent_id,
                name=server_name,
                reason=f"flow-tool default access '{access_mode}' (publisher-selected)",
                trace_headers=trace_headers,
            )

        metrics.publish_total.labels("publish", "ok").inc()
        metrics.publish_duration_seconds.observe(time.monotonic() - started)
        logger.info("tool_published", slug=slug, version=version, is_update=is_update)
        return {
            "slug": slug,
            "server_name": server_name,
            "tool_name": snake_name,
            "version": version,
            "invoke_url": f"{self._settings.bridge_base_url.rstrip('/')}/w/{slug}",
            "access_mode": access_mode,
            "is_update": is_update,
        }

    # ── test a published tool (run it with sample args, owner-only) ──────────
    async def test_tool(
        self, principal: Principal, slug: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        async def _get(conn):
            return await queries.get_binding_with_runtime(conn, slug)

        binding = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if binding is None or binding["status"] != "active":
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")

        try:
            schema_validate.validate(args, binding["input_schema"])
        except schema_validate.SchemaViolation as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Input schema validation failed: {exc.message}",
                status_code=422,
                details={"pointer": exc.pointer, "reason": exc.message},
            ) from exc

        secret = resolve_secret(binding["invoke_secret_ref"], self._settings)
        try:
            result = await invoke_workflow(
                self._http,
                internal_host=binding["internal_host"],
                http_node_root=binding["http_node_root"],
                http_path=binding["http_path"],
                method=binding["http_method"],
                args=args,
                secret=secret,
                secret_header=self._settings.nodered_invoke_secret_header,
                timeout=self._settings.nodered_invoke_timeout_seconds,
            )
        except NoderedError as exc:
            status = 502 if exc.retryable else 422
            code = ErrorCode.SERVICE_UNAVAILABLE if exc.retryable else ErrorCode.VALIDATION_ERROR
            raise ApiError(code, exc.message, status_code=status) from exc
        return {"tool": binding["snake_name"], "result": result}

    # ── list Node-RED flows (publish picker) ─────────────────────────────────
    async def list_flows(self, principal: Principal) -> list[dict[str, str]]:
        runtime = await ensure_runtime(
            self._pool, principal.tenant_id, self._provisioner, self._settings
        )
        admin_token = resolve_secret(runtime["admin_token_ref"], self._settings)
        return await self._admin.list_flow_tabs(
            internal_host=runtime["internal_host"], admin_token=admin_token
        )

    # ── list / get / unpublish ───────────────────────────────────────────────
    async def list_tools(self, principal: Principal) -> list[dict[str, Any]]:
        async def _list(conn):
            return await queries.list_bindings(conn)

        rows = await db_pool.in_tenant(self._pool, principal.tenant_id, _list)
        return [_public_view(r) for r in rows]

    async def get_tool(self, principal: Principal, slug: str) -> dict[str, Any]:
        async def _get(conn):
            return await queries.get_binding_by_slug(conn, slug)

        row = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")
        return _public_view(row)

    async def unpublish(
        self,
        principal: Principal,
        user_jwt: str,
        slug: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async def _get(conn):
            return await queries.get_binding_by_slug(conn, slug)

        row = await db_pool.in_tenant(self._pool, principal.tenant_id, _get)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")

        async def _retire(conn):
            await queries.set_binding_status(conn, row["binding_id"], "retired")

        await db_pool.in_tenant(self._pool, principal.tenant_id, _retire)

        # The registry has no hard delete — best-effort mark restricted so agents can't call
        # it; the binding retirement makes /manifest + /invoke return 404 (registry health
        # -> offline).
        if principal.agent_id:
            try:
                await self._registry.mark_restricted(
                    user_jwt=user_jwt,
                    agent_id=principal.agent_id,
                    name=row["manifest"]["name"],
                    reason="flow-tool unpublished",
                    trace_headers=trace_headers,
                )
            except ApiError as exc:
                logger.warning("unpublish_restrict_failed", slug=slug, error=exc.message)

        metrics.publish_total.labels("unpublish", "ok").inc()
        return {"slug": slug, "status": "retired"}


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a binding row for the frontend (no secret refs / internal hosts)."""
    return {
        "slug": row["slug"],
        "server_name": row["manifest"]["name"],
        "tool_name": row["snake_name"],
        "display_name": row["display_name"],
        "description": row["description"],
        "version": row["version"],
        "access_mode": row["access_mode"],
        "status": row["status"],
        "node_red_flow_id": row.get("node_red_flow_id"),
        "input_schema": row["input_schema"],
        "output_schema": row.get("output_schema"),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }
