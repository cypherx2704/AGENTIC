"""Unit tests for dual-mode JWT verification in ``core.auth`` — no running Auth.

We generate an RSA keypair in-process (``cryptography``), expose its public key as a
JWKS signing key, and monkeypatch the module's signing-key lookup
(``auth.get_jwks_client``) so the gateway verifies tokens we mint with the matching
private key + ``kid``. Tokens are exercised through the real ``require_principal``
FastAPI dependency mounted on a tiny app, so the full request -> Principal path runs.

Cases:
  (a) EXTERNAL valid agent JWT (scope ``llm:invoke``)        -> 200, Principal populated
  (b) missing ``llm:invoke`` scope                            -> 403 FORBIDDEN
  (c) bad ``iss`` / bad ``aud``                               -> 401 UNAUTHORIZED
  (d) expired                                                 -> 401 UNAUTHORIZED
  (e) INTERNAL: svc token + forwarded agent JWT, OBO matches  -> 200, service Principal
  (f) INTERNAL: on_behalf_of mismatch                         -> 401 UNAUTHORIZED
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from llms_gateway.core import auth as auth_module
from llms_gateway.core.auth import Principal, require_principal
from llms_gateway.core.config import Settings
from llms_gateway.core.errors import install_exception_handlers

_KID = "test-key-1"
# Read issuer/audience from a FRESH (non-cached) Settings so we never prime the
# process-wide ``get_settings()`` lru_cache — priming it here with the wrong
# MOCK_PROVIDERS value would contaminate the app-level chat/stream tests.
_SETTINGS = Settings()
ISSUER = _SETTINGS.auth_issuer_url
AUDIENCE = _SETTINGS.auth_platform_audience

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"


# ── In-process RSA keypair + JWKS signing-key seam ────────────────────────────────
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk() -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return jwk


class _FakeJWKClient:
    """Stand-in for PyJWKClient that always returns our single in-process key."""

    def __init__(self) -> None:
        self._key = PyJWK.from_dict(_public_jwk())

    def get_signing_key_from_jwt(self, _token: str) -> PyJWK:
        return self._key


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the auth module's signing-key lookup to our in-process public key."""
    monkeypatch.setattr(auth_module, "get_jwks_client", lambda _url: _FakeJWKClient())


def _mint(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": _KID})


def _agent_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": AGENT,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "scopes": ["llm:invoke"],
    }
    claims.update(overrides)
    return claims


def _svc_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "svc:xagent",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "on_behalf_of": AGENT,
        "scopes": ["llm:invoke"],
    }
    claims.update(overrides)
    return claims


# ── Tiny app exposing the real dependency ─────────────────────────────────────────
def _make_client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "agent_id": principal.agent_id,
            "scopes": principal.scopes,
            "principal_type": principal.principal_type,
        }

    # raise_server_exceptions=False so ApiError flows through the exception handlers
    # instead of bubbling as a raw exception in the test client.
    return TestClient(app, raise_server_exceptions=False)


# ── (a) EXTERNAL valid agent JWT ──────────────────────────────────────────────────
def test_external_valid_agent_jwt_builds_principal() -> None:
    client = _make_client()
    token = _mint(_agent_claims())
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert body["agent_id"] == AGENT
    assert body["scopes"] == ["llm:invoke"]
    assert body["principal_type"] == "agent"


# ── (b) missing required scope -> 403 ─────────────────────────────────────────────
def test_missing_llm_invoke_scope_forbidden() -> None:
    client = _make_client()
    token = _mint(_agent_claims(scopes=["some:other"]))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── (c) bad iss / bad aud -> 401 ──────────────────────────────────────────────────
def test_bad_issuer_unauthorized() -> None:
    client = _make_client()
    token = _mint(_agent_claims(iss="http://evil.example"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_bad_audience_unauthorized() -> None:
    client = _make_client()
    token = _mint(_agent_claims(aud="not-the-platform"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (d) expired -> 401 ────────────────────────────────────────────────────────────
def test_expired_token_unauthorized() -> None:
    client = _make_client()
    now = int(time.time())
    # Well outside the 60s clock-skew leeway.
    token = _mint(_agent_claims(iat=now - 1000, exp=now - 600))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (e) INTERNAL mode, on_behalf_of matches -> ok ─────────────────────────────────
def test_internal_service_on_behalf_of_match_ok() -> None:
    client = _make_client()
    svc = _mint(_svc_claims(on_behalf_of=AGENT))
    agent = _mint(_agent_claims())
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {svc}", "X-Forwarded-Agent-JWT": agent},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert body["agent_id"] == AGENT
    assert body["principal_type"] == "service"


# ── (f) INTERNAL mode, on_behalf_of mismatch -> 401 ───────────────────────────────
def test_internal_service_on_behalf_of_mismatch_unauthorized() -> None:
    client = _make_client()
    svc = _mint(_svc_claims(on_behalf_of="99999999-9999-9999-9999-999999999999"))
    agent = _mint(_agent_claims())
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {svc}", "X-Forwarded-Agent-JWT": agent},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── extra: missing/malformed Authorization -> 401 ─────────────────────────────────
def test_missing_authorization_header_unauthorized() -> None:
    client = _make_client()
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
