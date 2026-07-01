"""Dual-mode JWT verification + WP03 verifier-side revocation MIRROR (no live Auth/Valkey).

An in-process RSA keypair + a fake JWKS signing-key lookup let us mint tokens the verifier
accepts, then drive the auth modes + the three revocation rules through a fake Valkey
injected on ``app.state.valkey``. Mirrors the proven SharedCore auth-unit/revocation seam,
adapted to this server's coarse scope (``tool:invoke``) and ``tws_*`` metrics.

Shared revocation scheme (must match Auth + the other services):
  <prefix>jti:{jti}         exists                          -> 401 TOKEN_REVOKED
  <prefix>kid:{kid}         exists                          -> 401 TOKEN_REVOKED
  <prefix>agent:{agent_id}  exists AND token.iat < epoch    -> 401 TOKEN_REVOKED
FAIL-OPEN: Valkey unavailable -> ACCEPT + skipped metric.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm
from prometheus_client import REGISTRY

from tool_web_search.core import auth as auth_module
from tool_web_search.core.config import Settings, get_settings
from tool_web_search.main import create_app

_KID = "test-key-1"
_SETTINGS = Settings()
ISSUER = _SETTINGS.auth_issuer_url
AUDIENCE = _SETTINGS.auth_platform_audience
PREFIX = _SETTINGS.revocation_key_prefix

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk() -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return jwk


class _FakeJWKClient:
    def __init__(self) -> None:
        self._key = PyJWK.from_dict(_public_jwk())

    def get_signing_key_from_jwt(self, _token: str) -> PyJWK:
        return self._key


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "get_jwks_client", lambda _url: _FakeJWKClient())


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mint(claims: dict[str, Any], *, kid: str = _KID) -> str:
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": kid})


def _agent_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": AGENT,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "scopes": ["tool:invoke", "tool:tool-web-search:invoke"],
    }
    claims.update(overrides)
    return claims


class _FakeValkey:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self._store = dict(store or {})
        self.gets: list[str] = []

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        self.gets.append(key)
        return self._store.get(key)

    async def ping(self) -> bool:
        return True


class _DownValkey:
    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        raise ConnectionError("valkey unreachable")

    async def ping(self) -> bool:
        return False


def _make_client(valkey: object | None) -> TestClient:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()  # run lifespan (wires a real ValkeyClient on app.state)
    if valkey is not None:
        app.state.valkey = valkey
    else:
        app.state.valkey = None
    return client


def _skipped_count() -> float:
    return REGISTRY.get_sample_value("tws_revocation_check_skipped_total") or 0.0


_INVOKE_BODY = {"args": {"query": "x"}}


def test_clean_agent_token_invokes() -> None:
    client = _make_client(_FakeValkey({}))
    resp = client.post(
        "/mcp/v1/invoke",
        json=_INVOKE_BODY,
        headers={"Authorization": f"Bearer {_mint(_agent_claims())}"},
    )
    assert resp.status_code == 200, resp.text
    client.__exit__(None, None, None)


def test_missing_coarse_scope_403() -> None:
    # A token without tool:invoke is rejected by require_principal (coarse scope).
    claims = _agent_claims(scopes=["something:else"])
    client = _make_client(_FakeValkey({}))
    resp = client.post(
        "/mcp/v1/invoke", json=_INVOKE_BODY, headers={"Authorization": f"Bearer {_mint(claims)}"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    client.__exit__(None, None, None)


def test_missing_fine_scope_403() -> None:
    # Coarse scope present but the fine per-server scope absent -> handler denies.
    claims = _agent_claims(scopes=["tool:invoke"])
    client = _make_client(_FakeValkey({}))
    resp = client.post(
        "/mcp/v1/invoke", json=_INVOKE_BODY, headers={"Authorization": f"Bearer {_mint(claims)}"}
    )
    assert resp.status_code == 403, resp.text
    client.__exit__(None, None, None)


def test_missing_auth_header_401() -> None:
    client = _make_client(_FakeValkey({}))
    resp = client.post("/mcp/v1/invoke", json=_INVOKE_BODY)
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    client.__exit__(None, None, None)


def test_revoked_jti_rejected() -> None:
    claims = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}jti:{claims['jti']}": "1"})
    client = _make_client(valkey)
    resp = client.post(
        "/mcp/v1/invoke", json=_INVOKE_BODY, headers={"Authorization": f"Bearer {_mint(claims)}"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"
    client.__exit__(None, None, None)


def test_poisoned_kid_rejected() -> None:
    claims = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}kid:{_KID}": "1"})
    client = _make_client(valkey)
    resp = client.post(
        "/mcp/v1/invoke", json=_INVOKE_BODY, headers={"Authorization": f"Bearer {_mint(claims)}"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"
    client.__exit__(None, None, None)


def test_valkey_down_fails_open() -> None:
    client = _make_client(_DownValkey())
    before = _skipped_count()
    resp = client.post(
        "/mcp/v1/invoke",
        json=_INVOKE_BODY,
        headers={"Authorization": f"Bearer {_mint(_agent_claims())}"},
    )
    assert resp.status_code == 200, resp.text  # fail-open: availability wins
    assert _skipped_count() == before + 1
    client.__exit__(None, None, None)
