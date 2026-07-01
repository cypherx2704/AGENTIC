"""Dual-mode JWT verification + per-endpoint scope gate (require_scopes).

In-process RSA keypair + a faked JWKS signing-key lookup let us mint tokens the
verifier accepts, exercised through the real ``require_principal`` / ``require_scopes``
dependencies on a tiny app. No running Auth.

Cases:
  (a) EXTERNAL valid agent JWT                         -> 200 (require_principal, no scope gate)
  (b) bad iss / bad aud                                -> 401
  (c) expired                                          -> 401
  (d) admin endpoint with skill:admin scope             -> 200
  (e) admin endpoint WITHOUT an admin scope            -> 403
  (f) INTERNAL: svc token + forwarded agent, OBO match -> 200 service Principal
  (g) INTERNAL: on_behalf_of mismatch                  -> 401
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

from skill_registry.core import auth as auth_module
from skill_registry.core.auth import ADMIN_SCOPES, Principal, require_principal, require_scopes
from skill_registry.core.config import Settings
from skill_registry.core.errors import install_exception_handlers

_KID = "test-key-1"
_SETTINGS = Settings()
ISSUER = _SETTINGS.auth_issuer_url
AUDIENCE = _SETTINGS.auth_platform_audience

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


def _mint(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": _KID})


def _agent_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": AGENT, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300,
        "tenant_id": TENANT, "agent_id": AGENT, "scopes": ["skill:invoke"],
    }
    claims.update(overrides)
    return claims


def _svc_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "svc:xagent", "iss": ISSUER, "aud": "*", "iat": now, "exp": now + 300,
        "on_behalf_of": AGENT, "scopes": ["skill:invoke"],
    }
    claims.update(overrides)
    return claims


def _make_client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    require_admin = require_scopes(ADMIN_SCOPES)

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {"tenant_id": principal.tenant_id, "type": principal.principal_type}

    @app.post("/admin")
    async def admin(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
        return {"ok": True, "tenant_id": principal.tenant_id}

    return TestClient(app, raise_server_exceptions=False)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_external_valid_agent_jwt() -> None:
    client = _make_client()
    resp = client.get("/whoami", headers=_auth(_mint(_agent_claims())))
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == TENANT


def test_bad_audience_401() -> None:
    client = _make_client()
    resp = client.get("/whoami", headers=_auth(_mint(_agent_claims(aud="wrong"))))
    assert resp.status_code == 401


def test_expired_401() -> None:
    client = _make_client()
    now = int(time.time())
    resp = client.get("/whoami", headers=_auth(_mint(_agent_claims(iat=now - 1000, exp=now - 500))))
    assert resp.status_code == 401


def test_missing_auth_header_401() -> None:
    client = _make_client()
    assert client.get("/whoami").status_code == 401


def test_admin_endpoint_with_admin_scope_200() -> None:
    client = _make_client()
    resp = client.post("/admin", headers=_auth(_mint(_agent_claims(scopes=["skill:admin"]))))
    assert resp.status_code == 200, resp.text


def test_admin_endpoint_without_admin_scope_403() -> None:
    client = _make_client()
    resp = client.post("/admin", headers=_auth(_mint(_agent_claims(scopes=["skill:invoke"]))))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_internal_service_token_with_forwarded_agent_200() -> None:
    client = _make_client()
    headers = _auth(_mint(_svc_claims()))
    headers["X-Forwarded-Agent-JWT"] = _mint(_agent_claims())
    resp = client.get("/whoami", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "service"


def test_internal_on_behalf_of_mismatch_401() -> None:
    client = _make_client()
    headers = _auth(_mint(_svc_claims(on_behalf_of="someone-else")))
    headers["X-Forwarded-Agent-JWT"] = _mint(_agent_claims())
    resp = client.get("/whoami", headers=headers)
    assert resp.status_code == 401
