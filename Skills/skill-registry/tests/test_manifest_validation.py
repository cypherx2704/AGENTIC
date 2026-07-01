"""Contract-4 manifest validation."""

from __future__ import annotations

import copy

import pytest

from skill_registry.core.errors import ApiError, ErrorCode
from skill_registry.services import manifest as m

_VALID = {
    "schema_version": "1.0.0",
    "protocol_version": "mcp/1.0",
    "name": "skill-web-search",
    "version": "1.2.0",
    "description": "Search the web.",
    "required_scopes": ["skill:invoke", "skill:skill-web-search:invoke"],
    "skills": [
        {
            "name": "web_search",
            "description": "Perform a web search.",
            "input_schema": {"type": "object"},
        }
    ],
}


def test_valid_manifest_passes() -> None:
    assert m.validate_manifest(copy.deepcopy(_VALID)) is not None


@pytest.mark.parametrize("missing", ["schema_version", "protocol_version", "name", "version", "skills"])
def test_missing_required_field_rejected(missing: str) -> None:
    manifest = copy.deepcopy(_VALID)
    del manifest[missing]
    with pytest.raises(ApiError) as exc:
        m.validate_manifest(manifest)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert exc.value.details["field"] == missing


def test_non_dash_case_name_rejected() -> None:
    manifest = copy.deepcopy(_VALID)
    manifest["name"] = "Skill_Web_Search"
    with pytest.raises(ApiError) as exc:
        m.validate_manifest(manifest)
    assert exc.value.details["field"] == "name"


def test_bad_protocol_version_rejected() -> None:
    manifest = copy.deepcopy(_VALID)
    manifest["protocol_version"] = "1.0"
    with pytest.raises(ApiError) as exc:
        m.validate_manifest(manifest)
    assert exc.value.details["field"] == "protocol_version"


def test_empty_skills_array_rejected() -> None:
    manifest = copy.deepcopy(_VALID)
    manifest["skills"] = []
    with pytest.raises(ApiError) as exc:
        m.validate_manifest(manifest)
    assert exc.value.details["field"] == "skills"


def test_non_snake_case_skill_name_rejected() -> None:
    manifest = copy.deepcopy(_VALID)
    manifest["skills"][0]["name"] = "WebSearch"
    with pytest.raises(ApiError) as exc:
        m.validate_manifest(manifest)
    assert exc.value.details["field"] == "skills[0].name"


def test_skill_missing_input_schema_rejected() -> None:
    manifest = copy.deepcopy(_VALID)
    del manifest["skills"][0]["input_schema"]
    with pytest.raises(ApiError):
        m.validate_manifest(manifest)


def test_unknown_optional_fields_tolerated() -> None:
    # Contract-4 is additionalProperties: true — extra fields must not fail validation.
    manifest = copy.deepcopy(_VALID)
    manifest["sandbox_class"] = "gvisor"
    manifest["skills"][0]["estimated_cost_usd"] = 0.01
    assert m.validate_manifest(manifest) is not None


def test_extract_required_scopes_declared() -> None:
    assert m.extract_required_scopes(_VALID) == ["skill:invoke", "skill:skill-web-search:invoke"]


def test_extract_required_scopes_default() -> None:
    manifest = copy.deepcopy(_VALID)
    del manifest["required_scopes"]
    assert m.extract_required_scopes(manifest) == ["skill:invoke", "skill:skill-web-search:invoke"]


def test_declared_capabilities() -> None:
    assert m.declared_capabilities(_VALID) == ["web_search"]
