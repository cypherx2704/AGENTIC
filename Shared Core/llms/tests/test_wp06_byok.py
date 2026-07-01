"""WP06 — BYOK (bring-your-own-key): sealed:v1 crypto, /v1/keys CRUD, key resolution.

Three layers, all deterministic and no live infra:

1. **Crypto** (``services/byok.py``) — pure unit tests with an explicit ``Settings``
   carrying a test KEK: ``seal`` -> ``sealed:v1:...``; ``unseal(seal(x)) == x``; a tampered
   blob / wrong KEK / unknown scheme raise ``ByokCryptoError``; ``env:NAME`` resolves from
   the environment; KEK unset -> BYOK disabled (``seal`` raises ``ByokDisabledError``,
   ``resolve_provider_key`` returns ``None``); pool ``None`` -> ``None``.

2. **/v1/keys endpoints** against a fake psycopg pool (duck-typing ``in_tenant``):
   register -> list -> rotate (grace) -> delete, asserting the response NEVER carries the
   raw secret and the persisted ``secret_ref`` is sealed. AuthZ: a non-admin token is 403;
   KEK unset -> 503.

3. **provider_for_request** — returns the mock provider in mock mode, and the shared
   platform-keyed adaptor when no BYOK key resolves (fail-open).
"""

from __future__ import annotations

import base64
import contextlib
import os
import uuid

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402
from llms_gateway.services import byok  # noqa: E402
from llms_gateway.services.router import ModelRouter, Resolution  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
_KEK = "a-test-key-encryption-passphrase-32+chars"


# ── 1. crypto ────────────────────────────────────────────────────────────────────
def _kek_settings() -> Settings:
    return Settings(byok_kek=_KEK)


def test_seal_prefix_and_roundtrip() -> None:
    s = _kek_settings()
    assert byok.is_enabled(s) is True
    sealed = byok.seal("sk-live-secret-123", s)
    assert sealed.startswith("sealed:v1:")
    assert byok.unseal(sealed, s) == "sk-live-secret-123"


def test_seal_is_nondeterministic_but_roundtrips() -> None:
    # Fresh random DEK + nonces each call -> two seals of the same plaintext differ,
    # yet both unseal to the same value (proves it's not a static encoding).
    s = _kek_settings()
    a, b = byok.seal("same", s), byok.seal("same", s)
    assert a != b
    assert byok.unseal(a, s) == byok.unseal(b, s) == "same"


def test_tampered_blob_raises() -> None:
    s = _kek_settings()
    sealed = byok.seal("secret", s)
    raw = bytearray(base64.b64decode(sealed[len("sealed:v1:"):]))
    raw[-1] ^= 0xFF  # flip a ciphertext byte -> GCM auth fails
    tampered = "sealed:v1:" + base64.b64encode(bytes(raw)).decode()
    with pytest.raises(byok.ByokCryptoError):
        byok.unseal(tampered, s)


def test_wrong_kek_raises() -> None:
    sealed = byok.seal("secret", _kek_settings())
    other = Settings(byok_kek="a-totally-different-kek-passphrase-here!!")
    with pytest.raises(byok.ByokCryptoError):
        byok.unseal(sealed, other)


def test_malformed_base64_and_unknown_scheme_raise() -> None:
    s = _kek_settings()
    with pytest.raises(byok.ByokCryptoError):
        byok.unseal("sealed:v1:not-valid-base64!!!", s)
    with pytest.raises(byok.ByokCryptoError):
        byok.unseal("ftp:whatever", s)


def test_env_ref_resolves_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _kek_settings()
    monkeypatch.setenv("BYOK_UNIT_ENV_SECRET", "env-secret-xyz")
    assert byok.unseal("env:BYOK_UNIT_ENV_SECRET", s) == "env-secret-xyz"


def test_env_ref_unset_raises() -> None:
    s = _kek_settings()
    with pytest.raises(byok.ByokCryptoError):
        byok.unseal("env:DEFINITELY_NOT_SET_VAR_123", s)


def test_disabled_when_kek_unset() -> None:
    s = Settings(byok_kek="")
    assert byok.is_enabled(s) is False
    with pytest.raises(byok.ByokDisabledError):
        byok.seal("x", s)


@pytest.mark.asyncio
async def test_resolve_provider_key_none_when_disabled_or_no_pool() -> None:
    disabled = Settings(byok_kek="")
    # Disabled -> None regardless of pool.
    assert await byok.resolve_provider_key(object(), "t", "openai", disabled) is None
    # Enabled but no pool wired -> None (unit path).
    assert await byok.resolve_provider_key(None, "t", "openai", _kek_settings()) is None


@pytest.mark.asyncio
async def test_resolve_provider_key_unseals_db_row() -> None:
    """A fake pool returns one active sealed key; the resolver unseals it (real crypto)."""
    s = _kek_settings()
    sealed = byok.seal("sk-tenant-byok", s)

    class _Cur:
        def __init__(self, rows): self._rows = rows
        async def execute(self, sql, params=None): return self
        async def fetchall(self): return self._rows

    class _Conn:
        def __init__(self, rows): self._rows = rows
        @contextlib.asynccontextmanager
        async def transaction(self): yield self
        async def execute(self, sql, params=None): return _Cur([])
        def cursor(self, *, row_factory=None): return _Cur(self._rows)

    class _Pool:
        def __init__(self, rows): self._rows = rows
        @contextlib.asynccontextmanager
        async def connection(self, **k): yield _Conn(self._rows)

    pool = _Pool([{"secret_ref": sealed, "priority": 100, "status": "active", "grace_until": None}])
    assert await byok.resolve_provider_key(pool, TEST_TENANT, "openai", s) == "sk-tenant-byok"


# ── 3. provider_for_request ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_provider_for_request_returns_mock_in_mock_mode() -> None:
    router = ModelRouter(Settings(mock_providers=True), pool=None)
    prov = await router.provider_for_request(Resolution("openai", "text-embedding-3-small"), "t")
    assert type(prov).__name__ == "MockProvider"


@pytest.mark.asyncio
async def test_provider_for_request_platform_adaptor_when_no_byok() -> None:
    # Non-mock, BYOK disabled, no pool -> the shared platform-keyed adaptor (fail-open).
    router = ModelRouter(
        Settings(mock_providers=False, byok_kek="", openai_api_key="sk-platform"), pool=None
    )
    res = Resolution("openai", "text-embedding-3-small")
    prov = await router.provider_for_request(res, "t")
    assert prov is router._providers["openai"]


# ── 2. /v1/keys endpoints (fake pool) ───────────────────────────────────────────────
def _admin_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT, agent_id="b",
        scopes=["llm:invoke", "tenant:admin"], principal_type="agent",
    )


def _plain_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT, agent_id="b", scopes=["llm:invoke"], principal_type="agent",
    )


class _KeysCursor:
    def __init__(self, owner):
        self._owner = owner
        self._res = None
    async def execute(self, sql, params=None):
        self._owner.captured.append((sql, params))
        self._res = self._owner.respond(sql, params)
        return self
    async def fetchone(self): return self._res
    async def fetchall(self): return self._res if isinstance(self._res, list) else []


class _KeysConn:
    def __init__(self, owner): self._owner = owner
    @contextlib.asynccontextmanager
    async def transaction(self): yield self
    async def execute(self, sql, params=None):
        self._owner.captured.append((sql, params))
        return _KeysCursor(self._owner)
    def cursor(self, *, row_factory=None): return _KeysCursor(self._owner)


class _KeysPool:
    """Fake pool returning canned tenant_provider_keys rows; records every SQL+params."""

    def __init__(self, key_id: str) -> None:
        self.key_id = key_id
        self.captured: list = []

    def respond(self, sql: str, params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO llms.tenant_provider_keys"):
            return {"key_id": self.key_id, "provider": "openai",
                    "priority": params[3], "status": "active", "grace_until": None}
        if s.startswith("SELECT key_id, provider, priority, status FROM"):
            return {"key_id": params[0], "provider": "openai", "priority": 100, "status": "active"}
        if s.startswith("UPDATE llms.tenant_provider_keys SET status = 'revoked'"):
            return {"key_id": params[0], "provider": "openai", "priority": 100,
                    "status": "revoked", "grace_until": None}
        if s.startswith("SELECT key_id, provider, priority, status, grace_until FROM"):
            return [{"key_id": self.key_id, "provider": "openai", "priority": 100,
                     "status": "active", "grace_until": None}]
        return None

    @contextlib.asynccontextmanager
    async def connection(self, **k):
        yield _KeysConn(self)


@pytest_asyncio.fixture
async def keys_client():  # type: ignore[no-untyped-def]
    """Factory: (principal_fn, kek) -> (app, ac, pool) with the fake keys pool wired."""
    managers: list = []

    async def _factory(principal_fn, kek: str, key_id: str = None):  # type: ignore[no-untyped-def]
        app = create_app()
        app.dependency_overrides[require_principal] = principal_fn
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        pool = _KeysPool(key_id or str(uuid.uuid4()))
        app.state.db_pool = pool
        app.state.valkey = None
        # Fresh per-test settings so the byok_kek override never leaks via the shared
        # get_settings() lru_cache singleton.
        app.state.settings = Settings()
        app.state.settings.byok_kek = kek
        ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await ac.__aenter__()
        managers.append(ac)
        return app, ac, pool

    yield _factory
    for m in reversed(managers):
        await m.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_keys_lifecycle_register_list_rotate_delete_no_secret(keys_client) -> None:  # type: ignore[no-untyped-def]
    key_id = str(uuid.uuid4())
    app, ac, pool = await keys_client(_admin_principal, _KEK, key_id)

    # register
    reg = await ac.post("/v1/keys", json={"provider": "openai", "secret": "sk-RAW-XYZ", "priority": 50})
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["key_id"] == key_id
    assert body["status"] == "active"
    assert body["priority"] == 50
    # The raw secret never appears in the response, and no 'secret' field is leaked.
    assert "sk-RAW-XYZ" not in reg.text
    assert "secret" not in body and "secret_ref" not in body
    # The persisted secret_ref is sealed (not plaintext).
    insert = next(c for c in pool.captured if " ".join(c[0].split()).startswith("INSERT"))
    assert insert[1][2].startswith("sealed:v1:")
    assert "sk-RAW-XYZ" not in insert[1][2]

    # list — no secrets, just metadata
    lst = await ac.get("/v1/keys")
    assert lst.status_code == 200, lst.text
    rows = lst.json()["data"]
    assert rows and all("secret" not in r and "secret_ref" not in r for r in rows)

    # rotate — old key flips to a grace window; response carries rotated_from + new metadata
    rot = await ac.post(f"/v1/keys/{key_id}/rotate", json={"secret": "sk-NEW-SECRET"})
    assert rot.status_code == 200, rot.text
    assert rot.json()["rotated_from"] == key_id
    assert "sk-NEW-SECRET" not in rot.text
    # The grace UPDATE used the configured grace-days interval.
    upd = next(c for c in pool.captured if "SET status = 'rotating'" in " ".join(c[0].split()))
    assert upd[1][0] == app.state.settings.byok_grace_days

    # delete — soft revoke
    dele = await ac.delete(f"/v1/keys/{key_id}")
    assert dele.status_code == 200, dele.text
    assert dele.json()["status"] == "revoked"


@pytest.mark.asyncio
async def test_keys_require_admin_scope(keys_client) -> None:  # type: ignore[no-untyped-def]
    app, ac, pool = await keys_client(_plain_principal, _KEK)
    resp = await ac.post("/v1/keys", json={"provider": "openai", "secret": "sk-x"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_keys_register_503_when_kek_unset(keys_client) -> None:  # type: ignore[no-untyped-def]
    app, ac, pool = await keys_client(_admin_principal, "")  # BYOK disabled
    resp = await ac.post("/v1/keys", json={"provider": "openai", "secret": "sk-x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
