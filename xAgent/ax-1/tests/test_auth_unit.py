"""Unit tests for inbound agent-JWT verification in ``core.auth`` — no running Auth.

We generate an RSA keypair in-process (``cryptography``), expose its public key as a
JWKS signing key, and monkeypatch the module's signing-key lookup
(``auth.get_jwks_client``) so the runtime verifies tokens we mint with the matching
private key + ``kid``. Tokens are exercised through the real ``require_principal``
FastAPI dependency mounted on a tiny app, so the full request -> Principal path runs
with no real Auth / JWKS / network.

xAgent is an EDGE service: the inbound credential is a BARE agent JWT in the
``Authorization`` header (there is NO X-Forwarded-Agent-JWT inbound mode here). Cases:

  (a) valid agent JWT with scope ``agent:execute``  -> Principal populated, raw_token kept
  (b) missing ``agent:execute`` scope               -> 403 FORBIDDEN
  (c) bad ``iss``                                    -> 401 UNAUTHORIZED
  (d) bad ``aud``                                    -> 401 UNAUTHORIZED
  (e) expired                                        -> 401 UNAUTHORIZED
  (f) missing tenant_id claim                        -> 401 UNAUTHORIZED
  (g) missing / malformed Authorization header       -> 401 UNAUTHORIZED
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from agent_runtime.core import auth as auth_module
from agent_runtime.core.auth import Principal, require_principal
from agent_runtime.core.config import Settings
from agent_runtime.core.errors import install_exception_handlers

_KID = "test-key-1"
# Read issuer/audience from a FRESH (non-cached) Settings so we never prime the
# process-wide get_settings() lru_cache with anything unexpected.
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
        "scopes": ["agent:execute"],
    }
    claims.update(overrides)
    return claims


# ── Tiny app exposing the real dependency ─────────────────────────────────────────
def _make_client(monkeypatch: Any) -> TestClient:
    # Route the auth module's signing-key lookup to our in-process public key.
    monkeypatch.setattr(auth_module, "get_jwks_client", lambda _url: _FakeJWKClient())

    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "agent_id": principal.agent_id,
            "scopes": principal.scopes,
            "principal_type": principal.principal_type,
            "raw_token": principal.raw_token,
        }

    # raise_server_exceptions=False so ApiError flows through the exception handlers
    # instead of bubbling as a raw exception in the test client.
    return TestClient(app, raise_server_exceptions=False)


# ── (a) valid agent JWT -> Principal ───────────────────────────────────────────────
def test_valid_agent_jwt_builds_principal(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims())
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert body["agent_id"] == AGENT
    assert body["scopes"] == ["agent:execute"]
    assert body["principal_type"] == "agent"
    # The verified bearer is preserved verbatim for X-Forwarded-Agent-JWT propagation.
    assert body["raw_token"] == token


def test_valid_agent_jwt_with_space_delimited_scopes(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims(scopes="agent:execute agent:read"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert "agent:execute" in resp.json()["scopes"]


# ── (b) missing required scope -> 403 ──────────────────────────────────────────────
def test_missing_agent_execute_scope_forbidden(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims(scopes=["agent:read"]))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── (c) bad iss -> 401 ──────────────────────────────────────────────────────────────
def test_bad_issuer_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims(iss="http://evil.example"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (d) bad aud -> 401 ──────────────────────────────────────────────────────────────
def test_bad_audience_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims(aud="not-the-platform"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (e) expired -> 401 ──────────────────────────────────────────────────────────────
def test_expired_token_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    now = int(time.time())
    # Well outside the 60s clock-skew leeway.
    token = _mint(_agent_claims(iat=now - 1000, exp=now - 600))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (f) missing tenant_id claim -> 401 ──────────────────────────────────────────────
def test_missing_tenant_id_claim_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    claims = _agent_claims()
    del claims["tenant_id"]
    token = _mint(claims)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ── (g) missing / malformed Authorization header -> 401 ─────────────────────────────
def test_missing_authorization_header_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_malformed_authorization_scheme_unauthorized(monkeypatch: Any) -> None:
    client = _make_client(monkeypatch)
    token = _mint(_agent_claims())
    resp = client.get("/whoami", headers={"Authorization": f"Token {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
