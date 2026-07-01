"""Contract-4 (MCP) manifest validation.

A lightweight, dependency-free validator for the subset of the MCP manifest schema
(contracts/mcp/manifest.schema.json) the registry needs to enforce at registration
time. We deliberately do NOT pull a full JSON-Schema engine: the contract is
``additionalProperties: true`` (forward-compatible), so we validate only the REQUIRED
top-level fields, their formats (dash-case name, semver, mcp/x.y protocol), and the
``skills`` array (>= 1 skill, each with snake_case name + description + input_schema).

Validation failures raise a Contract-2 ``VALIDATION_ERROR`` (400) with the offending
field in ``details`` so a registrant gets an actionable message.
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import ApiError, ErrorCode

# Manifest field patterns (mirrors contracts/mcp/manifest.schema.json).
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # dash-case server name
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PROTOCOL_RE = re.compile(r"^mcp/[0-9]+\.[0-9]+$")
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")  # snake_case skill name

_REQUIRED_TOP = ("schema_version", "protocol_version", "name", "version", "description", "skills")


def _fail(message: str, field: str, value: Any = None) -> ApiError:
    details: dict[str, Any] = {"field": field}
    if value is not None:
        details["value"] = value
    return ApiError(ErrorCode.VALIDATION_ERROR, message, details=details)


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Validate ``manifest`` against the Contract-4 shape; return it on success.

    Raises ``ApiError(VALIDATION_ERROR)`` on the first violation found.
    """
    if not isinstance(manifest, dict):
        raise _fail("Manifest must be a JSON object.", "manifest")

    for key in _REQUIRED_TOP:
        if key not in manifest:
            raise _fail(f"Manifest missing required field '{key}'.", key)

    schema_version = manifest["schema_version"]
    if not (isinstance(schema_version, str) and _SEMVER_RE.match(schema_version)):
        raise _fail("schema_version must be semver (e.g. '1.0.0').", "schema_version", schema_version)

    protocol_version = manifest["protocol_version"]
    if not (isinstance(protocol_version, str) and _PROTOCOL_RE.match(protocol_version)):
        raise _fail(
            "protocol_version must match 'mcp/<major>.<minor>'.", "protocol_version", protocol_version
        )

    name = manifest["name"]
    if not (isinstance(name, str) and _NAME_RE.match(name)):
        raise _fail("name must be dash-case (e.g. 'skill-web-search').", "name", name)

    version = manifest["version"]
    if not (isinstance(version, str) and _SEMVER_RE.match(version)):
        raise _fail("version must be semver (e.g. '1.2.0').", "version", version)

    description = manifest["description"]
    if not (isinstance(description, str) and description.strip()):
        raise _fail("description must be a non-empty string.", "description")

    skills = manifest["skills"]
    if not (isinstance(skills, list) and skills):
        raise _fail("skills must be a non-empty array.", "skills")

    for idx, skill in enumerate(skills):
        if not isinstance(skill, dict):
            raise _fail("Each skill must be an object.", f"skills[{idx}]")
        tname = skill.get("name")
        if not (isinstance(tname, str) and _SKILL_NAME_RE.match(tname)):
            raise _fail("Skill name must be snake_case (e.g. 'web_search').", f"skills[{idx}].name", tname)
        tdesc = skill.get("description")
        if not (isinstance(tdesc, str) and tdesc.strip()):
            raise _fail("Skill description must be a non-empty string.", f"skills[{idx}].description")
        ischema = skill.get("input_schema")
        if not isinstance(ischema, dict):
            raise _fail("Skill input_schema must be an object.", f"skills[{idx}].input_schema")

    return manifest


def extract_required_scopes(manifest: dict[str, Any]) -> list[str]:
    """Return the manifest's declared ``required_scopes`` (capability rows).

    Falls back to the coarse ``skill:invoke`` plus the per-server fine scope
    ``skill:<name>:invoke`` when the manifest does not declare them explicitly (the
    Contract-4 default granularity).
    """
    declared = manifest.get("required_scopes")
    if isinstance(declared, list) and declared:
        return [str(s) for s in declared]
    name = manifest.get("name", "")
    return ["skill:invoke", f"skill:{name}:invoke"]


def declared_capabilities(manifest: dict[str, Any]) -> list[str]:
    """Return the per-skill capability identifiers (the snake_case skill names).

    These are the invocable capabilities a skill server exposes (Contract-4 ``skills[]``),
    persisted to ``skill_capabilities`` so discovery can advertise them.
    """
    caps: list[str] = []
    for skill in manifest.get("skills", []):
        if isinstance(skill, dict) and isinstance(skill.get("name"), str):
            caps.append(skill["name"])
    return caps
