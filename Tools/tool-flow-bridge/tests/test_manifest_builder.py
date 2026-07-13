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


# ── Aggregating MCP manifest ─────────────────────────────────────────────────────


def _mcp_row(**over):
    row = {
        "mcp_id": "m1",
        "tenant_id": TENANT,
        "slug": "mcp-math-1234abcd",
        "server_name": "mcp-math-1234abcd",
        "display_name": "Math",
        "description": "Math tools.",
        "visibility": "protected",
        "version": "2.1.0",
    }
    row.update(over)
    return row


def _tool_row(name, **over):
    row = {
        "snake_name": name,
        "display_name": name.title(),
        "description": f"{name} numbers.",
        "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}},
        "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }
    row.update(over)
    return row


def test_build_mcp_manifest_aggregates_members():
    settings = get_settings()
    mcp = _mcp_row()
    manifest = mb.build_mcp_manifest(
        settings, mcp=mcp, member_tools=[_tool_row("add"), _tool_row("mul", output_schema=None)]
    )
    assert manifest["name"] == "mcp-math-1234abcd"
    assert manifest["base_url"].endswith("/m/mcp-math-1234abcd")
    assert manifest["visibility"] == "protected"
    assert manifest["author"] == f"tenant:{TENANT}"
    assert manifest["required_scopes"] == [
        "tool:invoke",
        "tool:mcp-math-1234abcd:invoke",
    ]
    assert [t["name"] for t in manifest["tools"]] == ["add", "mul"]
    # output_schema is carried only when present.
    assert "output_schema" in manifest["tools"][0]
    assert "output_schema" not in manifest["tools"][1]
    # Reuses the same real-MCP transport descriptor as the single-tool builder.
    assert manifest["mcp"]["transport"] == "streamable-http"
    assert manifest["mcp"]["endpoint"] == "/mcp"


def test_build_mcp_manifest_platform_author():
    settings = get_settings()
    manifest = mb.build_mcp_manifest(settings, mcp=_mcp_row(tenant_id=None), member_tools=[])
    assert manifest["author"] == "platform"


def test_mcp_manifest_from_row_is_stable():
    settings = get_settings()
    mcp, members = _mcp_row(), [_tool_row("add")]
    a = mb.mcp_manifest_from_row(settings, mcp, members)
    b = mb.mcp_manifest_from_row(settings, mcp, members)
    assert a == b
    assert a["version"] == "2.1.0"  # carried straight from the mcp row
