"""Discovery resolution (pure) — UNION shadowing, invoke URL, view assembly."""

from __future__ import annotations

from skill_registry.services import discovery


def _row(name: str, *, is_platform: bool, skill_id: str) -> dict:
    return {
        "skill_id": skill_id,
        "name": name,
        "tenant_id": None if is_platform else "t1",
        "status": "active",
        "latest_version": "1.0.0",
        "is_platform": is_platform,
    }


def test_tenant_skill_shadows_platform_of_same_name() -> None:
    rows = [
        _row("skill-web-search", is_platform=True, skill_id="plat"),
        _row("skill-web-search", is_platform=False, skill_id="tenant"),
        _row("skill-translate", is_platform=True, skill_id="plat2"),
    ]
    resolved = discovery.shadow_by_tenant_priority(rows)
    by_name = {r["name"]: r for r in resolved}
    # The tenant's own skill wins the name collision.
    assert by_name["skill-web-search"]["skill_id"] == "tenant"
    assert by_name["skill-web-search"]["is_platform"] is False
    # A platform-only skill is still visible.
    assert by_name["skill-translate"]["skill_id"] == "plat2"
    assert len(resolved) == 2


def test_shadowing_is_order_independent() -> None:
    # Platform row appearing AFTER the tenant row must not overwrite it.
    rows = [
        _row("skill-x", is_platform=False, skill_id="tenant"),
        _row("skill-x", is_platform=True, skill_id="plat"),
    ]
    resolved = discovery.shadow_by_tenant_priority(rows)
    assert resolved[0]["skill_id"] == "tenant"


def test_platform_only_passthrough() -> None:
    rows = [_row("skill-y", is_platform=True, skill_id="plat")]
    resolved = discovery.shadow_by_tenant_priority(rows)
    assert resolved[0]["is_platform"] is True


def test_resolve_invoke_url_from_manifest_base_url() -> None:
    url = discovery.resolve_invoke_url({"base_url": "http://svc:9000/"}, "skill-x")
    assert url == "http://svc:9000"


def test_resolve_invoke_url_falls_back_to_convention() -> None:
    assert discovery.resolve_invoke_url(None, "skill-web-search") == "http://skill-web-search:8080"
    assert discovery.resolve_invoke_url({}, "skill-z") == "http://skill-z:8080"


def test_build_skill_view_uses_manifest_required_scopes() -> None:
    view = discovery.build_skill_view(
        _row("skill-web-search", is_platform=True, skill_id="plat"),
        manifest={"base_url": "http://x:8080", "required_scopes": ["skill:invoke", "skill:x:invoke"]},
        resolved_version="1.2.0",
        capabilities=[{"capability": "web_search", "required_scope": "skill:x:invoke"}],
        health={"status": "active"},
    )
    assert view["owner"] == "platform"
    assert view["version"] == "1.2.0"
    assert view["invoke_url"] == "http://x:8080"
    assert view["required_scopes"] == ["skill:invoke", "skill:x:invoke"]
    assert view["capabilities"] == ["web_search"]
    assert view["health"] == "active"


def test_build_skill_view_falls_back_to_capability_scopes() -> None:
    view = discovery.build_skill_view(
        _row("skill-x", is_platform=False, skill_id="t"),
        manifest={"name": "skill-x"},  # no required_scopes declared
        resolved_version="1.0.0",
        capabilities=[
            {"capability": "a", "required_scope": "skill:skill-x:invoke"},
            {"capability": "b", "required_scope": "skill:skill-x:invoke"},
        ],
        health=None,
    )
    assert view["owner"] == "tenant"
    assert view["required_scopes"] == ["skill:skill-x:invoke"]  # deduped
    assert view["health"] == "unknown"  # no health row
