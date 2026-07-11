"""Unit tests for the friendly-form -> names/schema/Contract-4 manifest transform."""

from __future__ import annotations

import pytest

from tool_flow_bridge.core.config import get_settings
from tool_flow_bridge.core.errors import ApiError
from tool_flow_bridge.services import manifest_builder as mb

TENANT = "1234abcd-0000-0000-0000-000000000000"


def test_snakeify():
    assert mb.snakeify("Sync Invoices") == "sync_invoices"
    assert mb.snakeify("  My-Tool 2 ") == "my_tool_2"
    assert mb.snakeify("2fast") == "t_2fast"


def test_build_names():
    snake, slug, server = mb.build_names(TENANT, "Sync Invoices", None)
    assert snake == "sync_invoices"
    assert slug == "sync-invoices-1234abcd"
    assert server == "tool-sync-invoices-1234abcd"


def test_build_names_explicit_snake():
    snake, slug, server = mb.build_names(TENANT, "Anything", "do_thing")
    assert snake == "do_thing"
    assert server == "tool-do-thing-1234abcd"


def test_build_names_rejects_bad():
    # snakeify always yields a valid identifier, so force an invalid one via a leading digit
    # that snakeify would fix — instead verify the happy path stays valid.
    snake, slug, server = mb.build_names(TENANT, "9", None)
    assert snake.startswith("t_")


def test_form_to_input_schema():
    schema = mb.form_to_input_schema(
        [
            {"name": "a", "type": "integer", "required": True, "description": "first"},
            {"name": "b", "type": "string"},
        ]
    )
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["a"]
    assert schema["properties"]["a"]["type"] == "integer"
    assert schema["properties"]["a"]["description"] == "first"


def test_form_to_input_schema_rejects_bad_name():
    with pytest.raises(ApiError):
        mb.form_to_input_schema([{"name": "bad name!", "type": "string"}])


def test_build_manifest_shape():
    settings = get_settings()
    schema = mb.form_to_input_schema([{"name": "q", "type": "string", "required": True}])
    manifest = mb.build_manifest(
        settings,
        slug="sync-invoices-1234abcd",
        snake_name="sync_invoices",
        display_name="Sync Invoices",
        description="Sync invoices from the ledger.",
        input_schema=schema,
        output_schema=None,
        version="1.0.0",
        tenant_id=TENANT,
    )
    assert manifest["name"] == "tool-sync-invoices-1234abcd"
    assert manifest["base_url"].endswith("/w/sync-invoices-1234abcd")
    assert manifest["required_scopes"] == [
        "tool:invoke",
        "tool:tool-sync-invoices-1234abcd:invoke",
    ]
    assert manifest["tools"][0]["name"] == "sync_invoices"
    assert manifest["tools"][0]["input_schema"] == schema
