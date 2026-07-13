"""Shared test helpers (imported by conftest + test modules).

Kept OUT of conftest.py so test modules can ``import _helpers`` directly (pytest's
prepend import mode puts the tests dir on sys.path). conftest re-exports the fixture.
"""

from __future__ import annotations

from memory_service.core.auth import Principal
from memory_service.core.config import Settings
from memory_service.services.embeddings import EmbeddingClient

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000cc"
AGENT_A = "agent-aaaa"
AGENT_B = "agent-bbbb"


def make_principal(
    *,
    tenant_id: str = TEST_TENANT,
    agent_id: str | None = AGENT_A,
    user_id: str | None = None,
    scopes: list[str] | None = None,
    plan: str | None = None,
    principal_type: str = "agent",
) -> Principal:
    claims: dict[str, object] = {}
    if plan is not None:
        claims["plan"] = plan
    return Principal(
        tenant_id=tenant_id,
        agent_id=agent_id,
        scopes=scopes if scopes is not None else ["mem:read", "mem:write"],
        principal_type=principal_type,
        user_id=user_id,
        raw_claims=claims,
    )


def bind_principal(app, principal: Principal) -> None:
    """Make every authenticated route resolve to ``principal``.

    ``require_scope(scope)`` builds a dependency that calls the module-level
    ``require_principal`` BY NAME at request time (not via FastAPI ``Depends``), so the
    cleanest, scope-respecting override is to patch that module function to return our
    fake principal. The real scope check (``scope in principal.scopes``) still runs, so a
    principal missing ``mem:write`` still 403s on a write route. The app_client fixture
    restores the original on teardown.
    """
    import memory_service.core.auth as auth_mod

    async def _fake_require_principal(_request):  # type: ignore[no-untyped-def]
        return principal

    auth_mod.require_principal = _fake_require_principal  # type: ignore[assignment]


class FakeValkey:
    """In-memory ValkeyClient stand-in (same surface auth/quota/idempotency use)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.store[key] = value

    async def set_if_absent(
        self, key: str, value: str, *, ttl_seconds: int, timeout_seconds: float | None = None
    ) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def incr_with_expire(
        self, key: str, *, ttl_seconds: int, timeout_seconds: float | None = None
    ) -> int:
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n


class SpyEmbeddingClient(EmbeddingClient):
    """Deterministic embedder that COUNTS real (uncached) embeds.

    Counts at the ``_embed_uncached`` seam — BELOW the B2 content-hash cache — so
    ``embed_calls`` reflects gateway/mock consultations only: it stays flat on an
    idempotency replay (short-circuits before embedding) AND on a cache hit (served from
    Valkey). Accepts the Contract-12 forwarding kwargs the production client passes.
    """

    def __init__(self, settings: Settings, *, valkey: object | None = None) -> None:
        super().__init__(settings, valkey=valkey)
        self.embed_calls = 0

    async def _embed_uncached(  # type: ignore[override]
        self, texts: list[str], *, on_behalf_of: str | None = None, agent_jwt: str | None = None
    ):
        self.embed_calls += 1
        return await super()._embed_uncached(texts, on_behalf_of=on_behalf_of, agent_jwt=agent_jwt)
