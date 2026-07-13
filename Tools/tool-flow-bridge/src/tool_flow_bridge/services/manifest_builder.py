"""Turn the Publish-dialog form into names, JSON Schemas, and a Contract-4 manifest.

The user never writes MCP JSON: they name the tool, describe it, and define input/output
parameters in a friendly form. This module converts that into:

* ``snake_name`` — the MCP tool name (snake_case, Contract-4).
* ``slug`` — a globally-unique dash-case id; the registry server name is ``tool-<slug>``.
* ``input_schema`` / ``output_schema`` — JSON Schema (draft 2020-12).
* the full Contract-4 manifest, with ``base_url = {bridge_base_url}/w/<slug>`` (which drives
  both the registry's invoke URL and its health-poll target).
"""

from __future__ import annotations

import re
from typing import Any

from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .mcp_protocol import SUPPORTED_PROTOCOL_VERSIONS

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_ALLOWED_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def snakeify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if s and s[0].isdigit():
        s = f"t_{s}"
    return s or "tool"


def _tenant_short(tenant_id: str) -> str:
    return tenant_id.replace("-", "")[:8].lower()


def build_names(tenant_id: str, title: str, snake_name: str | None) -> tuple[str, str, str]:
    """Return (snake_name, slug, server_name). Raises 422 on an invalid snake_name."""
    name = snakeify(snake_name) if snake_name else snakeify(title)
    if not _SNAKE_RE.match(name):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Tool name '{name}' is not a valid snake_case identifier.",
            status_code=422,
        )
    slug = f"{name.replace('_', '-')}-{_tenant_short(tenant_id)}"
    if not _SLUG_RE.match(slug):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Derived slug '{slug}' is invalid.", status_code=422)
    return name, slug, f"tool-{slug}"


def build_mcp_names(tenant_id: str, title: str, slug_hint: str | None = None) -> tuple[str, str]:
    """Return (slug, server_name) for a USER-created MCP collection. Tenant MCPs are named
    ``mcp-<snakeslug>-<tenant8>`` and the registry ``server_name`` == the slug. Raises 422 on an
    invalid derived slug."""
    base = snakeify(slug_hint) if slug_hint else snakeify(title)
    dashed = base.replace("_", "-")
    slug = f"mcp-{dashed}-{_tenant_short(tenant_id)}"
    if not _SLUG_RE.match(slug):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Derived MCP slug '{slug}' is invalid.", status_code=422)
    return slug, slug


def singleton_mcp_names(tool_slug: str) -> tuple[str, str]:
    """Return (slug, server_name) for the auto-created SINGLETON MCP wrapping one standalone tool.
    ``server_name = tool-<tool_slug>`` PRESERVES the registry key already used by the legacy
    single-tool wire + the 0004 data-migration backfill; the slug == the tool's slug so ``/m/<slug>``
    and the legacy ``/w/<slug>`` resolve the same collection."""
    return tool_slug, f"tool-{tool_slug}"


def platform_mcp_names(slug: str, tenant_id: str) -> tuple[str, str]:
    """Return (slug, server_name) for the PLATFORM (public) form of an MCP being promoted. Strips
    the ``-<tenant8>`` suffix and guarantees an ``mcp-`` prefix so the public server is named
    ``mcp-<snakeslug>`` (Contract: public MCPs live in the platform, tenant_id-NULL namespace)."""
    base = slug
    suffix = f"-{_tenant_short(tenant_id)}"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    if not base.startswith("mcp-"):
        base = f"mcp-{base}"
    if not _SLUG_RE.match(base):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, f"Derived platform MCP slug '{base}' is invalid.", status_code=422
        )
    return base, base


def _param_schema(param: dict[str, Any]) -> dict[str, Any]:
    ptype = param.get("type", "string")
    if ptype not in _ALLOWED_TYPES:
        ptype = "string"
    spec: dict[str, Any] = {"type": ptype}
    if param.get("description"):
        spec["description"] = str(param["description"])
    for key in ("enum",):
        if isinstance(param.get(key), list) and param[key]:
            spec[key] = param[key]
    for key in ("minimum", "maximum", "minLength", "maxLength"):
        if isinstance(param.get(key), int | float):
            spec[key] = param[key]
    if ptype == "array":
        item_type = param.get("items_type", "string")
        spec["items"] = {"type": item_type if item_type in _ALLOWED_TYPES else "string"}
    return spec


def form_to_input_schema(params: list[dict[str, Any]] | None) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in params or []:
        pname = str(p.get("name", "")).strip()
        if not pname or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", pname):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Invalid input parameter name '{pname}'.",
                status_code=422,
            )
        properties[pname] = _param_schema(p)
        if p.get("required"):
            required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def form_to_output_schema(params: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not params:
        return None
    properties: dict[str, Any] = {}
    for p in params:
        pname = str(p.get("name", "")).strip()
        if not pname:
            continue
        properties[pname] = _param_schema(p)
    if not properties:
        return None
    return {"type": "object", "properties": properties}


def build_manifest(
    settings: Settings,
    *,
    slug: str,
    snake_name: str,
    display_name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    version: str,
    tenant_id: str,
) -> dict[str, Any]:
    server_name = f"tool-{slug}"
    tool: dict[str, Any] = {
        "name": snake_name,
        "description": description,
        "input_schema": input_schema,
        "timeout_seconds": settings.tool_timeout_seconds,
        "idempotent": False,
    }
    if output_schema is not None:
        tool["output_schema"] = output_schema
    return {
        "schema_version": settings.manifest_schema_version,
        "protocol_version": settings.manifest_protocol_version,
        "name": server_name,
        "display_name": display_name,
        "version": version,
        "description": description,
        "author": f"tenant:{tenant_id}",
        "category": "flow-tool",
        "tags": ["flow-tool", "node-red", "no-code"],
        "base_url": f"{settings.bridge_base_url.rstrip('/')}/w/{slug}",
        "auth_required": True,
        "required_scopes": ["tool:invoke", f"tool:{server_name}:invoke"],
        "tools": [tool],
        # Real-MCP transport descriptor: base_url is {bridge}/w/<slug>, so the MCP endpoint
        # resolves to {base_url}/mcp — the sole tool wire xAgent invokes.
        "mcp": {
            "transport": "streamable-http",
            "endpoint": "/mcp",
            "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        },
        "health_endpoint": "/livez",
        "metrics_endpoint": "/metrics",
    }


def manifest_from_binding(settings: Settings, binding: dict[str, Any]) -> dict[str, Any]:
    """Regenerate the Contract-4 manifest deterministically from a stored binding row.

    The manifest is a PROJECTION of (binding fields + the platform generator), never a frozen
    snapshot. Regenerating it on read means a platform-side manifest change (e.g. adding the
    ``mcp`` descriptor, a new field, a schema-version bump) is reflected automatically — the
    ETag changes, the Tool Registry's poll picks it up, and agents see it — with NO re-publish
    and NO tool-version churn (the ``version`` is carried straight from the binding). Every
    input below is a persisted ``tool_bindings`` column.
    """
    return build_manifest(
        settings,
        slug=binding["slug"],
        snake_name=binding["snake_name"],
        display_name=binding["display_name"],
        description=binding["description"],
        input_schema=binding["input_schema"],
        output_schema=binding.get("output_schema"),
        version=binding["version"],
        tenant_id=str(binding["tenant_id"]),
    )


# ── Aggregating MCP manifest (a collection registered as ONE server) ──────────────────


def _member_tool_entry(settings: Settings, tool: dict[str, Any]) -> dict[str, Any]:
    """One Contract-4 ``tools[]`` entry from an atomic-tool row."""
    entry: dict[str, Any] = {
        "name": tool["snake_name"],
        "description": tool["description"],
        "input_schema": tool["input_schema"],
        "timeout_seconds": settings.tool_timeout_seconds,
        "idempotent": False,
    }
    if tool.get("output_schema") is not None:
        entry["output_schema"] = tool["output_schema"]
    return entry


def build_mcp_manifest(
    settings: Settings, *, mcp: dict[str, Any], member_tools: list[dict[str, Any]]
) -> dict[str, Any]:
    """Contract-4 manifest for an MCP (aggregating server). ``name`` is the MCP's ``server_name``
    (the registry key), ``tools[]`` is one entry per member tool, and the transport descriptor is
    the SAME real-MCP ``mcp`` block the single-tool builder emits — the base is ``/m/<slug>`` so the
    MCP endpoint resolves to ``{base_url}/mcp``. Carries ``visibility`` so the Marketplace can
    section servers. ``author`` is ``platform`` for a public/platform MCP (tenant_id NULL), else
    ``tenant:<id>``."""
    server_name = mcp["server_name"]
    tenant_id = mcp.get("tenant_id")
    author = "platform" if tenant_id is None else f"tenant:{tenant_id}"
    return {
        "schema_version": settings.manifest_schema_version,
        "protocol_version": settings.manifest_protocol_version,
        "name": server_name,
        "display_name": mcp["display_name"],
        "version": mcp["version"],
        "description": mcp["description"],
        "author": author,
        "category": "flow-tool",
        "tags": ["flow-tool", "node-red", "no-code", "mcp"],
        "base_url": f"{settings.bridge_base_url.rstrip('/')}/m/{mcp['slug']}",
        "auth_required": True,
        "required_scopes": ["tool:invoke", f"tool:{server_name}:invoke"],
        "visibility": mcp["visibility"],
        "tools": [_member_tool_entry(settings, t) for t in member_tools],
        # Real-MCP transport descriptor: base_url is {bridge}/m/<slug>, so the MCP endpoint
        # resolves to {base_url}/mcp — the aggregating tool wire xAgent invokes.
        "mcp": {
            "transport": "streamable-http",
            "endpoint": "/mcp",
            "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        },
        "health_endpoint": "/livez",
        "metrics_endpoint": "/metrics",
    }


def mcp_manifest_from_row(
    settings: Settings, mcp_row: dict[str, Any], member_tool_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Regenerate an aggregating MCP manifest deterministically from persisted rows (mirrors
    ``manifest_from_binding``): a live PROJECTION, not a frozen snapshot, so a platform-side
    generator change surfaces automatically via the ETag with NO re-publish and NO version churn
    (the ``version`` is carried straight from the mcp row). STABLE for a given input."""
    return build_mcp_manifest(settings, mcp=mcp_row, member_tools=member_tool_rows)
