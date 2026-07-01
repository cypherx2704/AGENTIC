"""Shared pytest configuration + fixtures for the agent-runtime test-suite.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is
the earliest deterministic place to pin the environment. The runtime caches its
``Settings`` via an ``lru_cache`` on ``get_settings()``; whichever code path calls it
first wins for the whole process. Pinning harmless localhost values here (DB / Kafka /
Auth / downstream URLs all default to localhost in ``core.config``) guarantees the
app-level tests resolve deterministic config and never need a real Auth, JWKS, Kafka,
DB, Guardrails, or LLMs gateway — regardless of test-module import order.

Every app-level test follows the SAME seam the llms-gateway + guardrails suites use:

  * ``require_principal`` is overridden via ``app.dependency_overrides`` so no real JWT
    / JWKS verification runs (identity is injected as a fixed :class:`Principal`).
  * ``app.state.db_pool`` is set to ``None`` after the lifespan opens, so the RLS /
    persistence path no-ops (there is no Postgres under test).
  * the downstream Guardrails / LLMs / service-token HTTP calls are respx-mocked.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

# ── Pin a harmless environment before anything imports the app / Settings ─────────
# All of these already default to localhost in core.config; setdefault keeps any value
# an outer harness may have exported but guarantees a deterministic baseline.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://xagent_user:localdev@localhost:5432/cypherx_platform",
)
os.environ.setdefault("KAFKA_BROKERS", "localhost:9092")
# Never start the real aiokafka producer under test: the app-level `client` fixture runs
# the full lifespan, and a real producer's connect/teardown across many function-scoped
# event loops can wedge the suite on Windows. Events still land in xagent.outbox; they are
# simply not drained in-process (the persistence path is no-op'd by db_pool = None anyway).
os.environ.setdefault("OUTBOX_PUBLISHER_ENABLED", "false")
# Never open the real Postgres pool under test: pool.open() spawns a libpq-backed worker
# whose C-level socket op does not yield to asyncio cancellation, wedging the
# function-scoped event-loop teardown (_cancel_all_tasks stuck in _select). The `client`
# fixture nulls app.state.db_pool after startup anyway, so the pool is unused in tests.
os.environ.setdefault("DB_POOL_OPEN_AT_STARTUP", "false")
# Never start the WP08 backup sweeper under test: its lifespan-scheduled loop would touch
# the (unopened, then nulled) DB pool across the many function-scoped event loops the suite
# churns through — the same wedge hazard the outbox publisher avoids. The asyncio.timeout
# guard on the request path is exercised directly in unit tests; the sweeper's own logic is
# unit-tested by constructing a TaskSweeper with a fake/None pool, not via the lifespan.
os.environ.setdefault("SWEEPER_ENABLED", "false")
os.environ.setdefault("AUTH_JWKS_URL", "http://localhost:8080/.well-known/jwks.json")
os.environ.setdefault("AUTH_ISSUER_URL", "http://localhost:8080")
os.environ.setdefault("AUTH_PLATFORM_AUDIENCE", "cypherx-platform")
os.environ.setdefault("AUTH_SERVICE_URL", "http://localhost:8080")
os.environ.setdefault("LLMS_GATEWAY_URL", "http://localhost:8085")
os.environ.setdefault("GUARDRAILS_SERVICE_URL", "http://localhost:8086")
os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "test-xagent-bootstrap-secret")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from agent_runtime.core.auth import Principal, require_principal  # noqa: E402
from agent_runtime.main import create_app  # noqa: E402
from agent_runtime.services.valkey import RevocationState  # noqa: E402

# Fixed identity injected by the overridden auth dependency (UUID-shaped so it matches
# the UUID columns / claims the real code would carry — Contract 13: identity from JWT).
TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TEST_AGENT_JWT = "test.inbound.agent-jwt"


def make_principal(
    *,
    tenant_id: str = TEST_TENANT,
    agent_id: str = TEST_AGENT,
    scopes: list[str] | None = None,
    raw_token: str = TEST_AGENT_JWT,
) -> Principal:
    """Build a fixed Principal carrying the required ``agent:execute`` scope."""
    return Principal(
        tenant_id=tenant_id,
        agent_id=agent_id,
        scopes=scopes if scopes is not None else ["agent:execute"],
        principal_type="agent",
        raw_token=raw_token,
        raw_claims={"tenant_id": tenant_id, "agent_id": agent_id},
    )


class _FakeValkey:
    """Network-free Valkey double for app-driven tests (the soft-dep analogue of
    ``db_pool = None``).

    The lifespan wires a *real* lazy ``ValkeyClient`` (``redis://localhost:6379``) that
    opens a live socket on first use — e.g. the ``/readyz`` soft ping. Under
    ``ASGITransport`` (in-process) tests that real connection is unnecessary, and across
    the many function-scoped event loops a full suite churns through, redis.asyncio's
    socket teardown can wedge on Windows (the probe stops honouring its 2s timeout and
    hangs). Swapping in this double keeps every app-driven test deterministic and
    infra-free while still answering the soft-probe + revocation-mirror surface.
    """

    async def ping(self) -> bool:
        return False  # readyz reports valkey "fail" — soft, never gates readiness

    async def aclose(self) -> None:
        return None

    async def revocation_lookup(self, **_kwargs: object) -> RevocationState:
        # Never reached via the ``client`` fixture (require_principal is overridden), but
        # provided so the double satisfies the full revocation seam if ever invoked.
        return RevocationState(jti_revoked=False, kid_revoked=False, agent_epoch=None)


@pytest.fixture
def principal() -> Principal:
    """The fixed test Principal (override target for ``require_principal``)."""
    return make_principal()


@pytest_asyncio.fixture
async def client(principal: Principal) -> AsyncIterator[AsyncClient]:
    """ASGI client with auth overridden + the DB pool dropped (no real DB needed).

    Mirrors the llms-gateway / guardrails ``client`` fixture: build the app, override
    ``require_principal`` to inject a fixed Principal, run the (DB-/Kafka-fail-soft)
    lifespan, then null the pool so the persistence path no-ops under test. A generous
    startup timeout covers the lifespan's bounded best-effort DB warm-up.
    """
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    async with LifespanManager(app, startup_timeout=20):
        app.state.db_pool = None  # no DB -> RLS / persistence path no-ops
        # Swap the lifespan's real lazy ValkeyClient for a network-free double: the
        # /readyz soft ping (and any revocation lookup) must not open a live redis socket
        # under in-process ASGITransport, where many function-scoped loops can wedge
        # redis.asyncio teardown on Windows (a real-Valkey ping hangs the full suite).
        app.state.valkey = _FakeValkey()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
