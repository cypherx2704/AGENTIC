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
        "health_endpoint": "/livez",
        "metrics_endpoint": "/metrics",
    }
