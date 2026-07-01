"""MCP manifest (Contract 4) — load the committed source-of-truth, ETag it, and validate
tool inputs against each tool's ``input_schema`` with a dependency-free validator.

The committed ``manifest.json`` (MANIFEST_PATH) is the single source of truth; this module
loads it once, exposes the per-tool input schemas, and computes a strong content-addressed
ETag for ``GET /manifest`` (304 support).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from ..core.config import get_settings


class SchemaViolation(Exception):
    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer
        self.message = message


@lru_cache
def load_manifest() -> dict[str, Any]:
    path = get_settings().manifest_path
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache
def tools_by_name() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in load_manifest().get("tools", [])}


def build_manifest() -> dict[str, Any]:
    return load_manifest()


def manifest_etag(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return '"' + hashlib.sha256(canonical).hexdigest() + '"'


def validate_input(tool_name: str, args: dict[str, Any]) -> None:
    """Validate ``args`` against the tool's ``input_schema`` (type/required/minLength/
    minimum/maximum/additionalProperties). Raises :class:`SchemaViolation` with a JSON
    Pointer to the offending field."""
    tool = tools_by_name().get(tool_name)
    if tool is None:
        raise SchemaViolation("/", f"unknown tool '{tool_name}'")
    schema = tool.get("input_schema", {})
    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    additional = schema.get("additionalProperties", True)

    if additional is False:
        for key in args:
            if key not in props:
                raise SchemaViolation(f"/{key}", f"unexpected property '{key}'")

    for field in required:
        if field not in args:
            raise SchemaViolation(f"/{field}", f"missing required property '{field}'")

    for key, spec in props.items():
        if key not in args:
            continue
        _validate_value(f"/{key}", args[key], spec)


def _validate_value(pointer: str, value: Any, spec: dict[str, Any]) -> None:
    import re

    expected = spec.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise SchemaViolation(pointer, "expected string")
        if "minLength" in spec and len(value) < spec["minLength"]:
            raise SchemaViolation(pointer, f"shorter than minLength {spec['minLength']}")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise SchemaViolation(pointer, f"longer than maxLength {spec['maxLength']}")
        if "pattern" in spec and re.search(spec["pattern"], value) is None:
            raise SchemaViolation(pointer, f"does not match pattern {spec['pattern']}")
    elif expected == "integer":
        # bool is a subclass of int — reject it as a non-integer.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaViolation(pointer, "expected integer")
        if "minimum" in spec and value < spec["minimum"]:
            raise SchemaViolation(pointer, f"less than minimum {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise SchemaViolation(pointer, f"greater than maximum {spec['maximum']}")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SchemaViolation(pointer, "expected number")
    elif expected == "array":
        if not isinstance(value, list):
            raise SchemaViolation(pointer, "expected array")
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise SchemaViolation(pointer, f"fewer than minItems {spec['minItems']}")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            raise SchemaViolation(pointer, f"more than maxItems {spec['maxItems']}")
