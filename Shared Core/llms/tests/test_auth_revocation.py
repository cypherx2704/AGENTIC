"""WP03 — verifier-side revocation MIRROR in ``core.auth`` (no running Auth/Valkey).

The gateway mirrors Auth's shared Valkey kill-switch AFTER signature/iss/aud/exp/scope
pass. We reuse the ``test_auth_unit`` seam — an in-process RSA keypair + a fake JWKS
signing-key lookup — so we can mint tokens the verifier accepts, then drive the three
revocation rules through a fake Valkey injected on ``app.state.valkey`` (no live Valkey).

Shared scheme (must match Auth + guardrails + xagent):
  <prefix>jti:{jti}         exists                          -> 401 TOKEN_REVOKED
  <prefix>kid:{kid}         exists                          -> 401 TOKEN_REVOKED
  <prefix>agent:{agent_id}  exists AND token.iat < epoch    -> 401 TOKEN_REVOKED
FAIL-OPEN: Valkey unavailable -> ACCEPT + log revocation_check_skipped + metric.

Cases:
  (a) revoked jti                          -> 401 TOKEN_REVOKED
  (b) poisoned kid                         -> 401 TOKEN_REVOKED
  (c) agent epoch newer than token.iat     -> 401 TOKEN_REVOKED
  (d) agent epoch OLDER than token.iat     -> 200 (token minted after the cascade)
  (e) clean token, Valkey reachable        -> 200
  (f) Valkey down (get raises)             -> 200 fail-open + skipped metric incremented
  (g) no Valkey client wired               -> 200 fail-open + skipped metric incremented
  (h) revocation_check_enabled = false     -> 200, Valkey never consulted
  (i) INTERNAL: forwarded agent jti revoked-> 401 (revoked agent can't slip via forwarding)
  (j) INTERNAL: service-token jti revoked  -> 401
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm
from prometheus_client import REGISTRY

from llms_gateway.core import auth as auth_module
from llms_gateway.core.auth import Principal, require_principal
from llms_gateway.core.config import Settings, get_settings
from llms_gateway.core.errors import install_exception_handlers

_KID = "test-key-1"
_SETTINGS = Settings()
ISSUER = _SETTINGS.auth_issuer_url
AUDIENCE = _SETTINGS.auth_platform_audience
PREFIX = _SETTINGS.revocation_key_prefix

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"


# ── In-process RSA keypair + JWKS signing-key seam (mirrors test_auth_unit) ──────────
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
    """Each test mutates Settings via get_settings(); start from a clean cache so the
    ``revocation_check_enabled=false`` case never leaks into the others."""
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
        "jti": str(uuid.uuid4()),
        "on_behalf_of": AGENT,
        "scopes": ["llm:invoke"],
    }
    claims.update(overrides)
    return claims


# ── Fake Valkey injected on app.state.valkey ─────────────────────────────────────────
class _FakeValkey:
    """In-memory stand-in mirroring ValkeyClient.get (decoded str | None)."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self._store = dict(store or {})
        self.gets: list[str] = []

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        self.gets.append(key)
        return self._store.get(key)


class _DownValkey:
    """Stand-in whose get() always raises — exercises the FAIL-OPEN path."""

    def __init__(self) -> None:
        self.gets: list[str] = []

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        self.gets.append(key)
        raise ConnectionError("valkey unreachable")


def _make_client(valkey: object | None) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    if valkey is not None:
        app.state.valkey = valkey

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {"tenant_id": principal.tenant_id, "agent_id": principal.agent_id}

    return TestClient(app, raise_server_exceptions=False)


def _skipped_count() -> float:
    return REGISTRY.get_sample_value("llms_revocation_check_skipped_total") or 0.0


def _rejected_count(rule: str) -> float:
    return REGISTRY.get_sample_value("llms_revocation_rejected_total", {"rule": rule}) or 0.0


# ── (a) revoked jti -> 401 ────────────────────────────────────────────────────────────
def test_revoked_jti_rejected() -> None:
    claims = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}jti:{claims['jti']}": "1"})
    client = _make_client(valkey)
    before = _rejected_count("jti")
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(claims)}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"
    assert _rejected_count("jti") == before + 1


# ── (b) poisoned kid -> 401 ───────────────────────────────────────────────────────────
def test_poisoned_kid_rejected() -> None:
    claims = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}kid:{_KID}": "1"})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(claims)}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── (c) agent epoch newer than token.iat -> 401 ──────────────────────────────────────
def test_agent_cascade_epoch_newer_than_iat_rejected() -> None:
    now = int(time.time())
    claims = _agent_claims(iat=now - 100)
    # Cascade epoch set AFTER the token was issued -> token predates the revoke -> reject.
    valkey = _FakeValkey({f"{PREFIX}agent:{AGENT}": str(now)})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(claims)}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── (d) agent epoch OLDER than token.iat -> token minted after the cascade -> 200 ─────
def test_agent_cascade_epoch_older_than_iat_passes() -> None:
    now = int(time.time())
    claims = _agent_claims(iat=now)
    valkey = _FakeValkey({f"{PREFIX}agent:{AGENT}": str(now - 100)})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(claims)}"})
    assert resp.status_code == 200, resp.text


# ── (e) clean token, Valkey reachable -> 200 ──────────────────────────────────────────
def test_clean_token_passes() -> None:
    valkey = _FakeValkey({})  # empty store: no revocation keys hit
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    # The jti and kid lookups were actually attempted.
    assert any(k.startswith(f"{PREFIX}jti:") for k in valkey.gets)
    assert any(k.startswith(f"{PREFIX}kid:") for k in valkey.gets)


# ── (f) Valkey down -> FAIL OPEN accept + skipped metric ──────────────────────────────
def test_valkey_down_fails_open_and_increments_metric() -> None:
    valkey = _DownValkey()
    client = _make_client(valkey)
    before = _skipped_count()
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text  # fail-open: availability wins
    assert _skipped_count() == before + 1


# ── (g) no Valkey client wired -> FAIL OPEN accept + skipped metric ───────────────────
def test_no_valkey_client_fails_open() -> None:
    client = _make_client(None)
    before = _skipped_count()
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    assert _skipped_count() == before + 1


# ── (h) revocation disabled -> 200, Valkey never consulted ────────────────────────────
def test_revocation_disabled_skips_valkey(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVOCATION_CHECK_ENABLED", "false")
    get_settings.cache_clear()
    # A revoked jti would normally 401 — disabled means it must NOT be consulted.
    claims = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}jti:{claims['jti']}": "1"})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(claims)}"})
    assert resp.status_code == 200, resp.text
    assert valkey.gets == []  # never queried


# ── (i) INTERNAL: forwarded agent JWT revoked -> 401 (no slip-through via forwarding) ─
def test_internal_forwarded_agent_jti_revoked_rejected() -> None:
    svc = _svc_claims(on_behalf_of=AGENT)
    agent = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}jti:{agent['jti']}": "1"})
    client = _make_client(valkey)
    resp = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {_mint(svc)}",
            "X-Forwarded-Agent-JWT": _mint(agent),
        },
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── (j) INTERNAL: service-token jti revoked -> 401 ────────────────────────────────────
def test_internal_service_token_jti_revoked_rejected() -> None:
    svc = _svc_claims(on_behalf_of=AGENT)
    agent = _agent_claims()
    valkey = _FakeValkey({f"{PREFIX}jti:{svc['jti']}": "1"})
    client = _make_client(valkey)
    resp = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {_mint(svc)}",
            "X-Forwarded-Agent-JWT": _mint(agent),
        },
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── INTERNAL clean dual-mode still passes with both tokens checked ────────────────────
def test_internal_clean_dual_mode_passes() -> None:
    svc = _svc_claims(on_behalf_of=AGENT)
    agent = _agent_claims()
    valkey = _FakeValkey({})
    client = _make_client(valkey)
    resp = client.get(
        "/whoami",
        headers={
            "Authorization": f"Bearer {_mint(svc)}",
            "X-Forwarded-Agent-JWT": _mint(agent),
        },
    )
    assert resp.status_code == 200, resp.text
    # Both the service token's jti AND the forwarded agent's jti were checked.
    assert f"{PREFIX}jti:{svc['jti']}" in valkey.gets
    assert f"{PREFIX}jti:{agent['jti']}" in valkey.gets
