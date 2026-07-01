"""Shared pytest configuration + the deterministic app-test harness.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is the
earliest deterministic place to pin the environment. The service caches its ``Settings``
via an ``lru_cache`` on ``get_settings()``; whichever code path calls it first wins for
the whole process. Pinning EMBEDDINGS_MOCK_FALLBACK + a harmless DATABASE_URL here
guarantees the app-level tests always use the deterministic offline embedder and never
need a real gateway, Auth, Kafka, or DB.

The ``app_client`` fixture builds the ASGI app, swaps in the in-memory repository, NULLs
the db_pool (so the publisher + resource-cap reads no-op), and provides a fresh per-test
FakeValkey/Settings so caps + quotas can be mutated live. Reusable helpers live in
``tests/_helpers.py`` so test modules can import them directly.
"""

from __future__ import annotations

import os

os.environ.setdefault("EMBEDDINGS_MOCK_FALLBACK", "true")
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://mem_user:localdev@localhost:5432/cypherx_platform"
)

import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from _helpers import FakeValkey, SpyEmbeddingClient  # noqa: E402
from memory_service.core.config import Settings  # noqa: E402
from memory_service.main import create_app  # noqa: E402
from memory_service.services import quota  # noqa: E402
from memory_service.services.repository import InMemoryRepository  # noqa: E402


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    import memory_service.core.auth as auth_mod
    import memory_service.main as main_mod

    original_require_principal = auth_mod.require_principal
    # The tests authenticate by patching ``require_principal`` (see ``bind_principal``), so
    # the real JWKS document is never needed. The production ``warm_jwks`` does a BLOCKING
    # urllib fetch of AUTH_JWKS_URL at startup; against the dead localhost JWKS host in the
    # offline test env that fetch stalls the event loop and trips the lifespan
    # startup_timeout (flaky setup TimeoutErrors). No-op it here — a test-only change that
    # never touches the production startup path.
    original_warm_jwks = main_mod.warm_jwks
    main_mod.warm_jwks = lambda _settings: None  # type: ignore[assignment]

    original_pool_create = main_mod.db_pool.create_pool

    def _fast_pool(database_url: str, **kwargs):  # type: ignore[no-untyped-def]
        # Tests run with no Postgres; keep min_size=0 so opening the pool never blocks the
        # lifespan on a connection to the dead localhost DB (it is NULLed below anyway).
        kwargs.setdefault("min_size", 0)
        return original_pool_create(database_url, **kwargs)

    main_mod.db_pool.create_pool = _fast_pool  # type: ignore[assignment]

    app = create_app()
    quota.clear_cache()
    async with LifespanManager(app, startup_timeout=30):
        # Deterministic, offline test wiring (overrides the lifespan's production wiring).
        # Stop the background outbox publisher so no AIOKafkaProducer is ever created.
        publisher = getattr(app.state, "outbox_publisher", None)
        if publisher is not None:
            await publisher.stop()
        app.state.db_pool = None  # publisher + resource-cap reads become no-ops
        app.state.repo = InMemoryRepository()
        app.state.settings = Settings()  # fresh per test (caps/quotas mutated live)
        app.state.embedder = SpyEmbeddingClient(app.state.settings)
        app.state.valkey = FakeValkey()
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield app, ac
        finally:
            # Restore the patched module functions so nothing leaks across tests.
            auth_mod.require_principal = original_require_principal
            main_mod.warm_jwks = original_warm_jwks
            main_mod.db_pool.create_pool = original_pool_create
