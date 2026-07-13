"""Dependency-free JSON-Schema-lite validator for invoke ``args``.

Generalizes the tool-web-search input validator to any tenant-authored ``input_schema``
(generated from the Publish dialog form). Covers the keyword set the manifest builder can
emit: ``type`` (string | integer | number | boolean | object | array), ``required``,
``minLength`` / ``maxLength``, ``minimum`` / ``maximum``, ``enum``, ``additionalProperties``.
On the first violation it raises :class:`SchemaViolation` with a JSON Pointer to the field.
"""

from __future__ import annotations

from typing import Any


class SchemaViolation(Exception):
    """Input-schema validation failure carrying a JSON Pointer to the offending field."""

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer
        self.message = message


def validate(instance: Any, schema: dict[str, Any], *, pointer: str = "") -> None:
    """Validate ``instance`` against ``schema`` (object-rooted). Raises SchemaViolation."""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")

    if expected == "object" or (expected is None and "properties" in schema):
        _validate_object(instance, schema, pointer)
        return

    _validate_scalar(instance, schema, pointer, expected)


def _validate_object(instance: Any, schema: dict[str, Any], pointer: str) -> None:
    if not isinstance(instance, dict):
        raise SchemaViolation(pointer or "", "Value must be a JSON object.")

    props: dict[str, Any] = schema.get("properties", {})

    for field in schema.get("required", []):
        if field not in instance:
            raise SchemaViolation(f"{pointer}/{field}", f"Missing required field '{field}'.")

    if schema.get("additionalProperties") is False:
        for field in instance:
            if field not in props:
                raise SchemaViolation(f"{pointer}/{field}", f"Unexpected field '{field}'.")

    for field, value in instance.items():
        spec = props.get(field)
        if isinstance(spec, dict):
            validate(value, spec, pointer=f"{pointer}/{field}")


def _validate_scalar(instance: Any, schema: dict[str, Any], pointer: str, expected: str | None) -> None:
    p = pointer or ""
    if expected == "string":
        if not isinstance(instance, str):
            raise SchemaViolation(p, "Value must be a string.")
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if isinstance(min_len, int) and len(instance) < min_len:
            raise SchemaViolation(p, f"Value must be at least {min_len} character(s).")
        if isinstance(max_len, int) and len(instance) > max_len:
            raise SchemaViolation(p, f"Value must be at most {max_len} character(s).")
    elif expected == "integer":
        # bool is a subclass of int — reject it as a non-integer.
        if not isinstance(instance, int) or isinstance(instance, bool):
            raise SchemaViolation(p, "Value must be an integer.")
        _check_range(instance, schema, p)
    elif expected == "number":
        if not isinstance(instance, int | float) or isinstance(instance, bool):
            raise SchemaViolation(p, "Value must be a number.")
        _check_range(instance, schema, p)
    elif expected == "boolean":
        if not isinstance(instance, bool):
            raise SchemaViolation(p, "Value must be a boolean.")
    elif expected == "array":
        if not isinstance(instance, list):
            raise SchemaViolation(p, "Value must be an array.")
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, elem in enumerate(instance):
                validate(elem, items, pointer=f"{p}/{idx}")

    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        raise SchemaViolation(p, f"Value must be one of {enum}.")


def _check_range(value: float, schema: dict[str, Any], pointer: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, int | float) and value < minimum:
        raise SchemaViolation(pointer, f"Value must be >= {minimum}.")
    if isinstance(maximum, int | float) and value > maximum:
        raise SchemaViolation(pointer, f"Value must be <= {maximum}.")
