"""WP06 — per-API-key ACL enforcement (Contract-18, ``services/acl.py``).

Deterministic, no live infra. ``enforce_acl`` is exercised directly against a fake
psycopg pool that returns canned ``api_key_acls`` rows (each row is the tuple
``(allowed_models, allowed_providers, allowed_operations)`` where each element is a
``list[str]`` or ``None`` = "no restriction on that dimension").

SEMANTICS asserted (Contract-18):
* a key with NO rows -> ALLOW (opt-in default, fail-open);
* ``pool=None`` / ACL disabled / no api_key_id -> ALLOW;
* a load error -> ALLOW (availability wins);
* a key WITH rows is allowed iff >=1 row permits model AND provider AND operation;
* otherwise 403 ``ACL_DENIED`` with the offending dimension (model > provider > op);
* the per-dimension NULL-vs-contains logic for each of the three columns.

An end-to-end case drives POST /v1/chat/completions with an api_key principal whose
ACL row excludes the resolved model -> the route returns 403 ACL_DENIED.
"""

from __future__ import annotations

import contextlib
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.core.errors import ApiError  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402
from llms_gateway.services.acl import enforce_acl  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
KEY_ID = "key-1111"

_MODEL = "claude-sonnet-4-6"
_PROVIDER = "anthropic"
_OP = "chat"


# ── fake pool returning canned ACL tuple rows ───────────────────────────────────────
class _Cur:
    def __init__(self, owner):
        self._owner = owner
        self._rows = None
    async def execute(self, sql, params=None):
        if "set_config" in sql and params:
            self._owner.set_config_tenant = params[0]
            self._rows = []
        else:
            self._rows = self._owner.rows
        return self
    async def fetchall(self): return self._rows or []


class _Conn:
    def __init__(self, owner): self._owner = owner
    @contextlib.asynccontextmanager
    async def transaction(self): yield self
    async def execute(self, sql, params=None):
        if "set_config" in sql and params:
            self._owner.set_config_tenant = params[0]
        return _Cur(self._owner)
    def cursor(self, *, row_factory=None): return _Cur(self._owner)


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.set_config_tenant = None
    @contextlib.asynccontextmanager
    async def connection(self, **k): yield _Conn(self)


class _RaisingPool:
    """A pool whose connection() blows up -> ACL load error -> fail open (allow)."""
    @contextlib.asynccontextmanager
    async def connection(self, **k):
        raise RuntimeError("db down")
        yield  # pragma: no cover


def _principal(api_key_id: str | None = KEY_ID) -> Principal:
    return Principal(
        tenant_id=TEST_TENANT, agent_id="a", scopes=["llm:invoke"],
        principal_type="api_key", api_key_id=api_key_id,
    )


async def _check(pool, rows_principal=None, *, model=_MODEL, provider=_PROVIDER, operation=_OP,
                 settings=None) -> str:
    """Run enforce_acl; return 'ALLOW' or 'DENY:<dimension>'."""
    p = rows_principal or _principal()
    s = settings or Settings(acl_enabled=True)
    try:
        await enforce_acl(pool, p, model=model, provider=provider, operation=operation, settings=s)
        return "ALLOW"
    except ApiError as e:
        assert e.status_code == 403
        assert e.details["reason"] == "ACL_DENIED"
        return f"DENY:{e.details['dimension']}"


# ── fail-open / allow cases ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_rows_allows() -> None:
    assert await _check(_Pool([])) == "ALLOW"


@pytest.mark.asyncio
async def test_pool_none_allows() -> None:
    assert await _check(None) == "ALLOW"


@pytest.mark.asyncio
async def test_no_api_key_id_allows_even_with_restrictive_rows() -> None:
    # No api_key_id on the principal -> the per-KEY ACL does not apply -> allow.
    restrictive = _Pool([(["gpt-4o"], None, None)])
    assert await _check(restrictive, _principal(api_key_id=None)) == "ALLOW"


@pytest.mark.asyncio
async def test_acl_disabled_allows() -> None:
    restrictive = _Pool([(["gpt-4o"], None, None)])
    assert await _check(restrictive, settings=Settings(acl_enabled=False)) == "ALLOW"


@pytest.mark.asyncio
async def test_load_error_fails_open() -> None:
    assert await _check(_RaisingPool()) == "ALLOW"


# ── permit / deny per dimension ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_row_permits_model_in_list() -> None:
    assert await _check(_Pool([([_MODEL], None, None)])) == "ALLOW"


@pytest.mark.asyncio
async def test_deny_model_not_in_list() -> None:
    assert await _check(_Pool([(["gpt-4o"], None, None)])) == "DENY:model"


@pytest.mark.asyncio
async def test_deny_provider_dimension() -> None:
    # Model unrestricted (NULL), provider restricted to openai but request is anthropic.
    assert await _check(_Pool([(None, ["openai"], None)])) == "DENY:provider"


@pytest.mark.asyncio
async def test_deny_operation_dimension() -> None:
    # Model + provider unrestricted; operation restricted to embedding, request is chat.
    assert await _check(_Pool([(None, None, ["embedding"])])) == "DENY:operation"


@pytest.mark.asyncio
async def test_null_dimensions_permit_everything() -> None:
    # All three NULL -> a wildcard row permits any triple.
    assert await _check(_Pool([(None, None, None)])) == "ALLOW"


@pytest.mark.asyncio
async def test_multiple_rows_any_one_permits() -> None:
    # First row excludes the model, second row permits it -> allowed (OR across rows).
    rows = [(["gpt-4o"], None, None), ([_MODEL], [_PROVIDER], [_OP])]
    assert await _check(_Pool(rows)) == "ALLOW"


@pytest.mark.asyncio
async def test_single_row_must_permit_all_three_dimensions() -> None:
    # One row permits the model but restricts operation to embedding -> denied (the SAME
    # row must satisfy all three; you can't mix-and-match across columns of one row).
    rows = [([_MODEL], None, ["embedding"])]
    assert await _check(_Pool(rows)) == "DENY:operation"


# ── end-to-end via the chat route ───────────────────────────────────────────────────
def _e2e_principal() -> Principal:
    return _principal()


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _e2e_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        # Fresh per-test settings (acl_enabled=True default) — isolate from any cap
        # mutations leaked by other test modules through the get_settings() singleton.
        app.state.settings = Settings()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield app, ac


@pytest.mark.asyncio
async def test_chat_returns_403_when_acl_denies_model(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # The key's only ACL row permits gpt-4o, but 'smart' resolves to claude-sonnet-4-6.
    app.state.db_pool = _Pool([(["gpt-4o"], None, None)])
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["code"] == "FORBIDDEN"
    assert err["details"]["reason"] == "ACL_DENIED"
    assert err["details"]["dimension"] == "model"


@pytest.mark.asyncio
async def test_chat_allowed_when_acl_permits(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _Pool([(["claude-sonnet-4-6"], ["anthropic"], ["chat"])])
    resp = await ac.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_embeddings_returns_403_when_acl_denies_operation(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # ACL permits only chat; the embeddings route uses operation='embedding'.
    app.state.db_pool = _Pool([(None, None, ["chat"])])
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["dimension"] == "operation"
