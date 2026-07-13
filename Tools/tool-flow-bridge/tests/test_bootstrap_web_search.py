"""web_search public-tool bootstrap wiring (Phase 5 · 5-websearch).

Exercises the REAL ``bootstrap_web_search`` orchestration (deploy flow -> publish tool+singleton MCP
-> promote to PUBLIC) against the same in-memory fakes the control-plane suite uses (FakeStore /
FakeRegistry / FakeAdmin). Proves the end state matches the task contract: a PUBLIC ``mcp-web-search``
server exposing the ``web_search`` tool, registered via ``register_platform`` (visibility=public,
author=platform), with the flow re-homed onto the platform runtime and the input schema
``{query:string (required), count?:integer}``.
"""

from __future__ import annotations

from tests.conftest import make_principal

# Reuse the fully-wired fakes + fixtures from the control-plane suite.
from tests.test_mcp_management import (  # noqa: F401 — `store`/`registry` are pytest fixtures
    ADMIN,
    FakeAdmin,
    FakeRegistry,
    registry,
    store,
)
from tool_flow_bridge.core.config import Settings
from tool_flow_bridge.services import bootstrap
from tool_flow_bridge.services.provisioner import (
    StaticPlatformProvisioner,
    StaticProvisioner,
)
from tool_flow_bridge.services.publisher import Publisher


def _publisher(reg: FakeRegistry) -> Publisher:
    """A real Publisher whose DB is the monkeypatched FakeStore (via the `store` fixture) and whose
    registry / Node-RED admin are fakes."""
    return Publisher(
        settings=Settings(provisioner_mode="static"),
        pool=None,  # unused — db_pool.in_tenant/in_platform are monkeypatched by `store`
        provisioner=StaticProvisioner(),
        registry=reg,
        nodered_admin=FakeAdmin(),
        http_client=None,
        platform_provisioner=StaticPlatformProvisioner(),
    )


async def test_bootstrap_publishes_and_promotes_public(store, registry) -> None:  # noqa: F811
    publisher = _publisher(registry)
    result = await bootstrap.bootstrap_web_search(
        publisher=publisher, principal=make_principal(ADMIN), user_jwt="user-jwt"
    )

    # 1. The tool is `web_search`.
    assert result["tool"]["snake_name"] == "web_search"

    # 2. The public MCP is registered via the PLATFORM path (never the tenant register path).
    public = result["public_mcp"]
    assert public["visibility"] == "public"
    assert public["slug"] == "mcp-web-search"  # tenant8 stripped by promote
    assert public["registry_status"] == "registered"
    assert public["runtime_rehomed"] is True
    assert registry.registrations, "publish registers the tenant singleton first"
    assert registry.platform_registrations, "promote registers via register_platform"
    reg = registry.platform_registrations[-1]
    assert reg["name"] == "mcp-web-search"
    assert reg["manifest"]["visibility"] == "public"
    assert reg["manifest"]["author"] == "platform"
    assert reg["manifest"]["base_url"].endswith("/m/mcp-web-search")
    # The public MCP exposes exactly the web_search tool with the {query, count} contract.
    tools = reg["manifest"]["tools"]
    assert [t["name"] for t in tools] == ["web_search"]
    input_schema = tools[0]["input_schema"]
    assert set(input_schema["properties"]) == {"query", "count"}
    assert input_schema["required"] == ["query"]
    assert input_schema["properties"]["count"]["type"] == "integer"


async def test_bootstrap_rehomes_flow_onto_platform_runtime(store, registry) -> None:  # noqa: F811
    publisher = _publisher(registry)
    admin = publisher._admin  # the FakeAdmin the deploy + re-home both use
    result = await bootstrap.bootstrap_web_search(
        publisher=publisher, principal=make_principal(ADMIN), user_jwt="user-jwt"
    )

    tool_id = result["tool"]["tool_id"]
    row = store.tools[tool_id]
    # Promote repoints the tool onto the platform runtime + flips it public.
    assert row["runtime_id"] == "platform-rt"
    assert row["visibility"] == "public"
    # deploy_flow (1) + promote re-home copy (1) => two create_flow calls; the tool points at the
    # platform copy.
    assert len(admin.created) == 2
    assert row["node_red_flow_id"] == admin.created[-1]["id"]
    # The old tenant server_name is de-registered now that Public is live.
    assert "tool-web-search-00000000" in registry.retirements


async def test_bootstrap_uses_packaged_flow_when_none_given(store, registry) -> None:  # noqa: F811
    """With no flow passed, the bootstrap deploys the packaged asset (the deploy still happens)."""
    publisher = _publisher(registry)
    admin = publisher._admin
    await bootstrap.bootstrap_web_search(
        publisher=publisher, principal=make_principal(ADMIN), user_jwt="user-jwt"
    )
    # The first create_flow is the deploy of the packaged single-flow object (has nodes).
    deployed = admin.created[0]["flow"]
    assert isinstance(deployed, dict)
    assert any(n.get("type") == "http in" for n in deployed.get("nodes", []))
