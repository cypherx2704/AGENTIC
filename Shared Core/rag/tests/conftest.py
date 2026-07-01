"""Shared pytest configuration + fixtures.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is the
earliest deterministic place to pin the environment. The service caches its ``Settings`` via
an ``lru_cache`` on ``get_settings()``; whichever code path calls it first wins for the whole
process. Pinning ``MOCK_EMBEDDINGS=true`` + a harmless ``DATABASE_URL`` here guarantees the
app-level tests resolve the deterministic mock embedder and never need a real llms, Auth,
Kafka, or DB.

The fixtures wire an in-memory fake DB pool (``tests/fakes.py``) that records every
``app.tenant_id`` set + answers the exact SQL the service issues — so ingest→query E2E, RLS
tenant isolation, ACL, and quota tests all run in-process with no Postgres/Valkey/Kafka.
"""

from __future__ import annotations

import contextlib
import os
from contextlib import asynccontextmanager

os.environ.setdefault("MOCK_EMBEDDINGS", "true")
os.environ.setdefault("EMBEDDINGS_FALLBACK_TO_MOCK", "true")
os.environ.setdefault("BOOTSTRAP_ENABLED", "false")  # the loop is started explicitly in tests
# Empty S3 endpoint -> the object store degrades put/head/get to no-op success (no network).
os.environ.setdefault("S3_ENDPOINT", "")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://rag_user:localdev@localhost:5432/cypherx_platform"
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from rag_service.core.config import get_settings  # noqa: E402
from rag_service.main import create_app  # noqa: E402

from .fakes import FakeDb, FakePool, FakeValkey  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000cc"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


@pytest.fixture
def fake_db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def fake_pool(fake_db: FakeDb) -> FakePool:
    return FakePool(fake_db)


@pytest.fixture
def fake_valkey() -> FakeValkey:
    return FakeValkey()


@asynccontextmanager
async def _make_app_client(fake_pool: FakePool, fake_valkey: FakeValkey):  # type: ignore[no-untyped-def]
    """Build an ASGI client with the fakes wired + the background loops disabled.

    Shared by ``app_client`` and the flag-variant fixtures. Callers that need non-default
    Settings set the env var + ``get_settings.cache_clear()`` BEFORE entering this CM so the
    lifespan resolves the overridden Settings.
    """
    import asyncio

    app = create_app()
    async with LifespanManager(app, startup_timeout=15, shutdown_timeout=20):
        # Stop the background publisher/sweeper that the lifespan started against the real
        # (unreachable) pool, then re-wire the in-memory fakes for the request path.
        await app.state.outbox_publisher.stop()
        await app.state.s3_sweeper.stop()
        # Eagerly close the real (unreachable) psycopg pool now so the lifespan's shutdown
        # pool.close() is a fast no-op. Without this its worker threads keep retrying the
        # dead localhost Postgres and the shutdown hangs (no live DB in this in-process suite).
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app.state.db_pool.close(), timeout=8)
        app.state.db_pool = fake_pool
        app.state.valkey = fake_valkey
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac._app = app  # type: ignore[attr-defined] — let tests reach app.state
            yield ac


@pytest_asyncio.fixture
async def app_client(fake_pool: FakePool, fake_valkey: FakeValkey):  # type: ignore[misc]
    """An ASGI client with the fake pool + valkey wired and the background loops disabled."""
    async with _make_app_client(fake_pool, fake_valkey) as ac:
        yield ac


@pytest_asyncio.fixture
async def app_client_rerank(monkeypatch, fake_pool: FakePool, fake_valkey: FakeValkey):  # type: ignore[misc]
    """Like ``app_client`` but with RAG_RERANK_ENABLED=true (mock reranker via mock_embeddings)."""
    monkeypatch.setenv("RAG_RERANK_ENABLED", "true")
    get_settings.cache_clear()
    async with _make_app_client(fake_pool, fake_valkey) as ac:
        yield ac


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset the plan-limits TTL cache between tests so quota tests don't bleed."""
    from rag_service.services import quota

    quota.clear_cache()
    get_settings.cache_clear()
    yield
    quota.clear_cache()


# ── Auth injection ────────────────────────────────────────────────────────────────
# Routes depend on require_scope(...) outputs (one dependency object per route), which all
# call core.auth.require_principal internally. Monkeypatching _resolve_principal +
# _enforce_revocation makes require_principal return our chosen principal WHILE still
# exercising the real require_scope scope-checking. The test sets app.state via a header.

def make_principal(
    *,
    tenant_id: str = TEST_TENANT,
    agent_id: str | None = TEST_AGENT,
    scopes: list[str] | None = None,
    principal_type: str = "agent",
    api_key_id: str | None = None,
    user_id: str | None = None,
    plan: str | None = None,
    limits: dict | None = None,
):
    from rag_service.core.auth import Principal

    claims: dict = {}
    if plan is not None:
        claims["plan"] = plan
    if limits is not None:
        claims["limits"] = limits
    return Principal(
        tenant_id=tenant_id,
        agent_id=agent_id,
        scopes=scopes if scopes is not None else ["rag:query", "rag:ingest", "rag:admin"],
        principal_type=principal_type,
        api_key_id=api_key_id,
        user_id=user_id,
        raw_claims=claims,
    )


@pytest.fixture
def auth_as(monkeypatch):
    """Return a setter that pins the principal returned by require_principal."""
    from rag_service.core import auth as auth_mod

    state = {"principal": make_principal()}

    def _resolve(request, settings):  # noqa: ANN001
        return state["principal"], []

    async def _enforce(request, settings, subjects):  # noqa: ANN001
        return None

    monkeypatch.setattr(auth_mod, "_resolve_principal", _resolve)
    monkeypatch.setattr(auth_mod, "_enforce_revocation", _enforce)

    def _set(principal) -> None:  # noqa: ANN001
        state["principal"] = principal

    return _set
