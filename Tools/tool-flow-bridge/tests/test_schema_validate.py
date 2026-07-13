"""Unit tests for the dependency-free JSON-Schema-lite validator."""

from __future__ import annotations

import pytest

from tool_flow_bridge.services import schema_validate as sv

SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "integer", "minimum": 0, "maximum": 10},
        "b": {"type": "string", "minLength": 1},
        "flag": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["x", "y"]},
    },
    "required": ["a"],
    "additionalProperties": False,
}


def test_accepts_valid():
    sv.validate({"a": 5, "b": "hi", "flag": True, "mode": "x"}, SCHEMA)


def test_missing_required():
    with pytest.raises(sv.SchemaViolation) as e:
        sv.validate({"b": "hi"}, SCHEMA)
    assert e.value.pointer == "/a"


def test_additional_property_rejected():
    with pytest.raises(sv.SchemaViolation) as e:
        sv.validate({"a": 1, "extra": 1}, SCHEMA)
    assert e.value.pointer == "/extra"


def test_type_mismatch_and_pointer():
    with pytest.raises(sv.SchemaViolation) as e:
        sv.validate({"a": "not-int"}, SCHEMA)
    assert e.value.pointer == "/a"


def test_bool_is_not_integer():
    with pytest.raises(sv.SchemaViolation):
        sv.validate({"a": True}, SCHEMA)


def test_range_and_minlength():
    with pytest.raises(sv.SchemaViolation):
        sv.validate({"a": 99}, SCHEMA)
    with pytest.raises(sv.SchemaViolation):
        sv.validate({"a": 1, "b": ""}, SCHEMA)


def test_enum():
    with pytest.raises(sv.SchemaViolation):
        sv.validate({"a": 1, "mode": "z"}, SCHEMA)
