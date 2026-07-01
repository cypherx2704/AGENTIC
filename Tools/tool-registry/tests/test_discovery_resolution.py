"""Discovery resolution (pure) — UNION shadowing, invoke URL, view assembly."""

from __future__ import annotations

from tool_registry.services import discovery


def _row(name: str, *, is_platform: bool, tool_id: str) -> dict:
    return {
        "tool_id": tool_id,
        "name": name,
        "tenant_id": None if is_platform else "t1",
        "status": "active",
        "latest_version": "1.0.0",
        "is_platform": is_platform,
    }


def test_tenant_tool_shadows_platform_of_same_name() -> None:
    rows = [
        _row("tool-web-search", is_platform=True, tool_id="plat"),
        _row("tool-web-search", is_platform=False, tool_id="tenant"),
        _row("tool-translate", is_platform=True, tool_id="plat2"),
    ]
    resolved = discovery.shadow_by_tenant_priority(rows)
    by_name = {r["name"]: r for r in resolved}
    # The tenant's own tool wins the name collision.
    assert by_name["tool-web-search"]["tool_id"] == "tenant"
    assert by_name["tool-web-search"]["is_platform"] is False
    # A platform-only tool is still visible.
    assert by_name["tool-translate"]["tool_id"] == "plat2"
    assert len(resolved) == 2


def test_shadowing_is_order_independent() -> None:
    # Platform row appearing AFTER the tenant row must not overwrite it.
    rows = [
        _row("tool-x", is_platform=False, tool_id="tenant"),
        _row("tool-x", is_platform=True, tool_id="plat"),
    ]
    resolved = discovery.shadow_by_tenant_priority(rows)
    assert resolved[0]["tool_id"] == "tenant"


def test_platform_only_passthrough() -> None:
    rows = [_row("tool-y", is_platform=True, tool_id="plat")]
    resolved = discovery.shadow_by_tenant_priority(rows)
    assert resolved[0]["is_platform"] is True


def test_resolve_invoke_url_from_manifest_base_url() -> None:
    url = discovery.resolve_invoke_url({"base_url": "http://svc:9000/"}, "tool-x")
    assert url == "http://svc:9000"


def test_resolve_invoke_url_falls_back_to_convention() -> None:
    assert discovery.resolve_invoke_url(None, "tool-web-search") == "http://tool-web-search:8080"
    assert discovery.resolve_invoke_url({}, "tool-z") == "http://tool-z:8080"


def test_build_tool_view_uses_manifest_required_scopes() -> None:
    view = discovery.build_tool_view(
        _row("tool-web-search", is_platform=True, tool_id="plat"),
        manifest={"base_url": "http://x:8080", "required_scopes": ["tool:invoke", "tool:x:invoke"]},
        resolved_version="1.2.0",
        capabilities=[{"capability": "web_search", "required_scope": "tool:x:invoke"}],
        health={"status": "active"},
    )
    assert view["owner"] == "platform"
    assert view["version"] == "1.2.0"
    assert view["invoke_url"] == "http://x:8080"
    assert view["required_scopes"] == ["tool:invoke", "tool:x:invoke"]
    assert view["capabilities"] == ["web_search"]
    assert view["health"] == "active"


def test_build_tool_view_falls_back_to_capability_scopes() -> None:
    view = discovery.build_tool_view(
        _row("tool-x", is_platform=False, tool_id="t"),
        manifest={"name": "tool-x"},  # no required_scopes declared
        resolved_version="1.0.0",
        capabilities=[
            {"capability": "a", "required_scope": "tool:tool-x:invoke"},
            {"capability": "b", "required_scope": "tool:tool-x:invoke"},
        ],
        health=None,
    )
    assert view["owner"] == "tenant"
    assert view["required_scopes"] == ["tool:tool-x:invoke"]  # deduped
    assert view["health"] == "unknown"  # no health row
