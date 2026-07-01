"""Shared pytest configuration + fixtures.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is the
earliest deterministic place to pin the environment. The server caches its ``Settings``
via an ``lru_cache`` on ``get_settings()``; whichever code path calls it first wins for
the whole process. Pinning ``SEARCH_PROVIDER=mock`` here guarantees app-level tests always
resolve the deterministic, network-free mock provider and never need a real Auth, Valkey,
or search-provider key.

Fixtures provided:

* :data:`FakeValkey` — an in-memory stand-in for ``ValkeyClient`` (the subset of commands
  the rate limiter + idempotency use). Tests inject it on ``app.state.valkey``.
* :func:`fake_principal` — a Principal carrying BOTH MCP scopes, used to override the
  ``require_principal`` dependency so no real JWT/JWKS is needed.
* :func:`make_client` — factory building an ASGI test client with a chosen Valkey and an
  optional Principal override (lets the scope-deny test drop a scope).
"""

from __future__ import annotations

import os

os.environ.setdefault("SEARCH_PROVIDER", "mock")
os.environ.setdefault("ENVIRONMENT", "test")

from collections.abc import Callable  # noqa: E402

import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from tool_web_search.core.auth import Principal, require_principal  # noqa: E402
from tool_web_search.main import create_app  # noqa: E402
from tool_web_search.services import manifest as manifest_svc  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"

# Both scopes a valid caller holds (Contract-4 dual scope).
BOTH_SCOPES = [manifest_svc.COARSE_SCOPE, manifest_svc.FINE_SCOPE]


def make_principal(scopes: list[str] | None = None) -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=list(scopes if scopes is not None else BOTH_SCOPES),
        principal_type="agent",
    )


class FakeValkey:
    """In-memory stand-in mirroring the ValkeyClient command subset used by the service."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(self, key, value, *, ttl_seconds=None, timeout_seconds=None) -> None:  # type: ignore[no-untyped-def]
        self.store[key] = value

    async def set_if_absent(self, key, value, *, ttl_seconds, timeout_seconds=None) -> bool:  # type: ignore[no-untyped-def]
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def incr_with_expire(self, key, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n


class DownValkey:
    """Stand-in whose commands always raise — exercises the FAIL-OPEN paths."""

    async def ping(self) -> bool:
        return False

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        raise ConnectionError("valkey unreachable")

    async def set(self, key, value, *, ttl_seconds=None, timeout_seconds=None) -> None:  # type: ignore[no-untyped-def]
        raise ConnectionError("valkey unreachable")

    async def set_if_absent(self, key, value, *, ttl_seconds, timeout_seconds=None) -> bool:  # type: ignore[no-untyped-def]
        raise ConnectionError("valkey unreachable")

    async def incr_with_expire(self, key, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        raise ConnectionError("valkey unreachable")


@pytest_asyncio.fixture
async def make_client() -> Callable:  # type: ignore[type-arg]
    """Yield an async factory: ``await make_client(valkey=..., principal=...)`` -> AsyncClient.

    ``valkey`` defaults to a fresh in-memory :class:`FakeValkey`; pass ``None`` for the
    no-Valkey fail-open path, or a :class:`DownValkey`. ``principal`` overrides the auth
    dependency (defaults to a Principal with BOTH scopes).
    """
    managers: list = []

    async def _factory(  # type: ignore[no-untyped-def]
        *, valkey: object | None = ..., principal: Principal | None = None
    ) -> AsyncClient:
        app = create_app()
        app.dependency_overrides[require_principal] = (
            (lambda: principal) if principal is not None else (lambda: make_principal())
        )
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        # ``...`` sentinel => default FakeValkey; explicit None => no Valkey wired.
        app.state.valkey = FakeValkey() if valkey is ... else valkey
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        await ac.__aenter__()
        managers.append(ac)
        ac.app = app  # type: ignore[attr-defined]  # expose for store inspection
        return ac

    yield _factory

    for m in reversed(managers):
        await m.__aexit__(None, None, None)
