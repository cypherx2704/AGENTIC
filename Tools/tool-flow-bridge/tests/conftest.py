"""Shared pytest fixtures. Pins a network-free test environment (harmless DATABASE_URL,
static provisioner) and provides an ASGI client factory that overrides ``require_principal``
so no real JWT/JWKS/DB is needed. DB-backed handlers are exercised by monkeypatching the
``db.pool`` helpers + ``queries`` in the individual tests.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://flow_tools_user:x@localhost:5432/none")
os.environ.setdefault("PROVISIONER_MODE", "static")

from collections.abc import Callable  # noqa: E402

import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from tool_flow_bridge.core.auth import Principal, require_principal  # noqa: E402
from tool_flow_bridge.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def make_principal(scopes: list[str] | None = None) -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=list(scopes if scopes is not None else ["tool:invoke"]),
        principal_type="agent",
    )


class FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(self, key, value, *, ttl_seconds=None, timeout_seconds=None) -> None:  # type: ignore[no-untyped-def]
        self.store[key] = value

    async def incr_with_expire(self, key, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n


@pytest_asyncio.fixture
async def make_client() -> Callable:  # type: ignore[type-arg]
    managers: list = []

    async def _factory(*, principal: Principal | None = None) -> AsyncClient:  # type: ignore[no-untyped-def]
        app = create_app()
        app.dependency_overrides[require_principal] = (
            (lambda: principal) if principal is not None else (lambda: make_principal())
        )
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        app.state.valkey = FakeValkey()
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        await ac.__aenter__()
        managers.append(ac)
        ac.app = app  # type: ignore[attr-defined]
        return ac

    yield _factory

    for m in reversed(managers):
        await m.__aexit__(None, None, None)
