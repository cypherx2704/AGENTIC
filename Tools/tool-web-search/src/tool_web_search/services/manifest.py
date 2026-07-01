"""Contract-4 MCP manifest + its JSON-Schema input validation.

:func:`build_manifest` assembles the Contract-4 manifest object for this server
(validates against contracts/mcp/manifest.schema.json): server ``name`` dash-case
(``tool-web-search``), one snake_case tool (``web_search``) with its ``input_schema``
and ``output_schema``, the declared ``required_scopes`` (``tool:invoke`` +
``tool:tool-web-search:invoke``), and the health/metrics endpoints.

:func:`manifest_etag` derives a stable, content-addressed ETag (sha256 of the canonical
JSON) so the registry's ``If-None-Match`` poll gets a 304 when the manifest is unchanged.

:func:`validate_input` validates invoke args against the tool's ``input_schema`` and, on
failure, returns a JSON Pointer to the offending field (e.g. ``/query``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core.config import Settings

# Server + tool naming per Contract 4 (server dash-case, tool snake_case).
SERVER_NAME = "tool-web-search"
TOOL_NAME = "web_search"

# The two scopes a caller must hold to invoke (Contract-4 dual-scope check).
COARSE_SCOPE = "tool:invoke"
FINE_SCOPE = "tool:tool-web-search:invoke"


def _input_schema(settings: Settings) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for the invoke args."""
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Search query.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": settings.max_max_results,
                "default": settings.default_max_results,
                "description": "Maximum number of results to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "snippet": {"type": "string"},
                        "rank": {"type": "integer"},
                    },
                },
            }
        },
    }


def build_manifest(settings: Settings) -> dict[str, Any]:
    """Return the Contract-4 MCP manifest object for this server."""
    return {
        "schema_version": settings.manifest_schema_version,
        "protocol_version": settings.manifest_protocol_version,
        "name": SERVER_NAME,
        "display_name": "Web Search",
        "version": settings.service_version,
        "description": "Search the web and return ranked results with snippets.",
        "author": "CypherX Platform",
        "category": "research",
        "tags": ["search", "web", "information"],
        "auth_required": True,
        "required_scopes": [COARSE_SCOPE, FINE_SCOPE],
        "tools": [
            {
                "name": TOOL_NAME,
                "description": "Perform a web search and return top results.",
                "input_schema": _input_schema(settings),
                "output_schema": _output_schema(),
                "timeout_seconds": settings.tool_timeout_seconds,
                "idempotent": True,
                "rate_limit": {"rpm": settings.rate_limit_requests_per_min, "rpd": 0},
            }
        ],
        "invoke_endpoint": "/mcp/v1/invoke",
        "health_endpoint": "/livez",
        "metrics_endpoint": "/metrics",
    }


def canonical_json(manifest: dict[str, Any]) -> str:
    """Stable canonical JSON serialization (sorted keys, compact) for hashing."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def manifest_etag(manifest: dict[str, Any]) -> str:
    """Content-addressed strong ETag: sha256 of the canonical JSON, quoted."""
    digest = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    return f'"{digest}"'


class SchemaViolation(Exception):
    """Input-schema validation failure carrying a JSON Pointer to the offending field."""

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer
        self.message = message


def validate_input(args: dict[str, Any], settings: Settings) -> None:
    """Validate invoke ``args`` against the tool ``input_schema``.

    Raises :class:`SchemaViolation` (carrying a JSON Pointer like ``/query``) on the
    first violation. A minimal, dependency-free validator covering the keywords this
    tool's schema uses: type, required, minLength, minimum, maximum, additionalProperties.
    """
    if not isinstance(args, dict):
        raise SchemaViolation("", "Invoke args must be a JSON object.")

    schema = _input_schema(settings)
    props: dict[str, Any] = schema["properties"]

    # required
    for field in schema.get("required", []):
        if field not in args:
            raise SchemaViolation(f"/{field}", f"Missing required field '{field}'.")

    # additionalProperties: False
    if schema.get("additionalProperties") is False:
        for field in args:
            if field not in props:
                raise SchemaViolation(f"/{field}", f"Unexpected field '{field}'.")

    # per-field constraints
    for field, value in args.items():
        spec = props.get(field)
        if spec is None:
            continue
        _validate_field(field, value, spec)


def _validate_field(field: str, value: Any, spec: dict[str, Any]) -> None:
    pointer = f"/{field}"
    expected = spec.get("type")

    if expected == "string":
        if not isinstance(value, str):
            raise SchemaViolation(pointer, f"Field '{field}' must be a string.")
        min_len = spec.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            raise SchemaViolation(
                pointer, f"Field '{field}' must be at least {min_len} character(s)."
            )
    elif expected == "integer":
        # bool is a subclass of int — reject it as a non-integer here.
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaViolation(pointer, f"Field '{field}' must be an integer.")
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            raise SchemaViolation(pointer, f"Field '{field}' must be >= {minimum}.")
        if isinstance(maximum, int) and value > maximum:
            raise SchemaViolation(pointer, f"Field '{field}' must be <= {maximum}.")
