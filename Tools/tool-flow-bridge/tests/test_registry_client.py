"""RegistryClient tests — Contract-12 tool-registry calls (respx-mocked, no real registry/Auth).

The ServiceTokenProvider is replaced with an in-process fake so no Auth ``/v1/service-tokens``
call is made; only the registry HTTP surface is mocked (respx). Every assertion runs against the
CURRENT :class:`RegistryClient` signatures — ``register(...)``, ``mark_restricted(...)``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tool_flow_bridge.core.config import Settings
from tool_flow_bridge.core.errors import ApiError, ErrorCode
from tool_flow_bridge.services.registry_client import RegistryClient

REGISTRY = "http://registry.test"
NAME = "flow.sum"
MANIFEST = {"name": NAME, "version": "1.0.0", "description": "d"}


class FakeTokenProvider:
    """Stands in for ServiceTokenProvider — records ``on_behalf_of`` and returns a static JWT."""

    def __init__(self, token: str = "svc-token") -> None:
        self.token = token
        self.calls: list[str | None] = []

    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        self.calls.append(on_behalf_of)
        return self.token


def _settings() -> Settings:
    return Settings(tool_registry_url=REGISTRY)


def _make_client(client: httpx.AsyncClient) -> tuple[RegistryClient, FakeTokenProvider]:
    tokens = FakeTokenProvider()
    return RegistryClient(_settings(), tokens, client), tokens  # type: ignore[arg-type]


# ── register(): first-publish create ─────────────────────────────────────────
@respx.mock
async def test_register_create_201_returns_json():
    route = respx.post(f"{REGISTRY}/v1/tools").mock(
        return_value=httpx.Response(201, json={"tool_id": "t1", "name": NAME})
    )
    async with httpx.AsyncClient() as http:
        client, tokens = _make_client(http)
        result = await client.register(
            user_jwt="user-jwt",
            agent_id="agent-1",
            name=NAME,
            manifest=MANIFEST,
            is_update=False,
        )
    assert result == {"tool_id": "t1", "name": NAME}
    # SERVICE token minted on behalf of the publishing agent; user JWT forwarded verbatim.
    assert tokens.calls == ["agent-1"]
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer svc-token"
    assert req.headers["X-Forwarded-Agent-JWT"] == "user-jwt"


# ── register(): 409 on create → append a version instead ─────────────────────
@respx.mock
async def test_register_409_falls_through_to_post_version():
    create = respx.post(f"{REGISTRY}/v1/tools").mock(return_value=httpx.Response(409))
    version = respx.post(f"{REGISTRY}/v1/tools/{NAME}/versions").mock(
        return_value=httpx.Response(201, json={"version": "1.0.1"})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.register(
            user_jwt="user-jwt",
            agent_id="agent-1",
            name=NAME,
            manifest=MANIFEST,
            is_update=False,
        )
    assert result == {"version": "1.0.1"}
    assert create.called
    assert version.called


# ── _post_version() via is_update=True: 200 and 201 both accepted ────────────
@pytest.mark.parametrize("status", [200, 201])
@respx.mock
async def test_post_version_200_or_201_ok(status: int):
    version = respx.post(f"{REGISTRY}/v1/tools/{NAME}/versions").mock(
        return_value=httpx.Response(status, json={"version": "2.0.0"})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.register(
            user_jwt="user-jwt",
            agent_id="agent-1",
            name=NAME,
            manifest=MANIFEST,
            is_update=True,
        )
    assert result == {"version": "2.0.0"}
    assert version.called
    assert version.calls.last.request.url.path == f"/v1/tools/{NAME}/versions"


# ── _post_version(): 404 (no tool to version) → fall back to create ──────────
@respx.mock
async def test_post_version_404_falls_back_to_create():
    version = respx.post(f"{REGISTRY}/v1/tools/{NAME}/versions").mock(
        return_value=httpx.Response(404, json={"error": {"message": "no such tool"}})
    )
    create = respx.post(f"{REGISTRY}/v1/tools").mock(
        return_value=httpx.Response(201, json={"tool_id": "t-new"})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.register(
            user_jwt="user-jwt",
            agent_id="agent-1",
            name=NAME,
            manifest=MANIFEST,
            is_update=True,
        )
    assert result == {"tool_id": "t-new"}
    assert version.called
    assert create.called


# ── register_platform(): platform-namespace create + 409→version fallback ─────
@respx.mock
async def test_register_platform_create_201():
    route = respx.post(f"{REGISTRY}/v1/platform/tools").mock(
        return_value=httpx.Response(201, json={"tool_id": "p1", "name": NAME, "owner": "platform"})
    )
    async with httpx.AsyncClient() as http:
        client, tokens = _make_client(http)
        result = await client.register_platform(
            user_jwt="user-jwt", agent_id="agent-1", name=NAME, manifest=MANIFEST
        )
    assert result == {"tool_id": "p1", "name": NAME, "owner": "platform"}
    assert route.called
    # Hits the PLATFORM namespace, not the tenant register path.
    assert route.calls.last.request.url.path == "/v1/platform/tools"


@respx.mock
async def test_register_platform_409_falls_through_to_version():
    create = respx.post(f"{REGISTRY}/v1/platform/tools").mock(return_value=httpx.Response(409))
    version = respx.post(f"{REGISTRY}/v1/platform/tools/{NAME}/versions").mock(
        return_value=httpx.Response(201, json={"version": "1.0.1", "owner": "platform"})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.register_platform(
            user_jwt="user-jwt", agent_id="agent-1", name=NAME, manifest=MANIFEST
        )
    assert result == {"version": "1.0.1", "owner": "platform"}
    assert create.called
    assert version.called


@respx.mock
async def test_register_platform_403_maps_to_forbidden():
    respx.post(f"{REGISTRY}/v1/platform/tools").mock(
        return_value=httpx.Response(403, json={"error": {"message": "platform:admin required"}})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        with pytest.raises(ApiError) as exc:
            await client.register_platform(
                user_jwt="user-jwt", agent_id="agent-1", name=NAME, manifest=MANIFEST
            )
    assert exc.value.code == ErrorCode.FORBIDDEN


# ── retire(): de-register a tool; 404 is idempotent (already gone) ────────────
@respx.mock
async def test_retire_200_returns_json():
    route = respx.post(f"{REGISTRY}/v1/tools/{NAME}/retire").mock(
        return_value=httpx.Response(200, json={"name": NAME, "status": "retired"})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.retire(user_jwt="user-jwt", agent_id="agent-1", name=NAME)
    assert result == {"name": NAME, "status": "retired"}
    assert route.called


@respx.mock
async def test_retire_404_is_idempotent():
    respx.post(f"{REGISTRY}/v1/tools/{NAME}/retire").mock(
        return_value=httpx.Response(404, json={"error": {"message": "not found"}})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.retire(user_jwt="user-jwt", agent_id="agent-1", name=NAME)
    # A missing/already-retired tool must NOT fail promote.
    assert result == {"name": NAME, "status": "not_found"}


# ── mark_restricted(): posts the expected body and tolerates 200/201/409 ─────
@pytest.mark.parametrize("status", [200, 201, 409])
@respx.mock
async def test_mark_restricted_tolerates_200_201_409(status: int):
    route = respx.post(f"{REGISTRY}/v1/restricted-tools/{NAME}").mock(
        return_value=httpx.Response(status, json={"ok": True})
    )
    async with httpx.AsyncClient() as http:
        client, _ = _make_client(http)
        result = await client.mark_restricted(
            user_jwt="user-jwt",
            agent_id="agent-1",
            name=NAME,
            reason="policy",
            default_access_mode="ask",
        )
    assert result is None
    assert route.called
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body == {"reason": "policy", "default_access_mode": "ask"}


async def _register_403():
    with respx.mock:
        respx.post(f"{REGISTRY}/v1/tools").mock(
            return_value=httpx.Response(403, json={"error": {"message": "not tool:admin"}})
        )
        async with httpx.AsyncClient() as http:
            client, _ = _make_client(http)
            await client.register(
                user_jwt="user-jwt",
                agent_id="agent-1",
                name=NAME,
                manifest=MANIFEST,
                is_update=False,
            )


# ── 403 anywhere maps to ApiError(FORBIDDEN) ─────────────────────────────────
async def test_403_raises_forbidden_api_error():
    with pytest.raises(ApiError) as exc:
        await _register_403()
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert exc.value.status_code == 403
    # The registry error message is surfaced from the response envelope.
    assert exc.value.message == "not tool:admin"
