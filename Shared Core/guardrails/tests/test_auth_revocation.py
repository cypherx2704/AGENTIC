"""Verifier-side revocation mirror (WP03) exercised through the real ``require_principal``.

Mirrors the llms-gateway auth-unit pattern: an in-process RSA keypair (``cryptography``)
backs a fake JWKS signing-key seam (``auth.get_jwks_client`` is monkeypatched), so tokens
minted here verify against the real dependency mounted on a tiny app — no live Auth/JWKS.
A fake Valkey is injected on ``app.state.valkey`` (the same accessor ``/readyz`` uses), so
NO live Valkey is required.

The shared revocation scheme (all four services agree): AFTER signature/iss/aud/exp/scope
pass, reject 401 TOKEN_REVOKED if ANY of these keys (under ``REVOCATION_KEY_PREFIX``) hit:
  * ``{prefix}jti:{jti}``          -> token revoked
  * ``{prefix}kid:{kid}``          -> signing key poisoned
  * ``{prefix}agent:{agent_id}``   -> unix-epoch; reject when ``token.iat < epoch``
FAIL-OPEN: a Valkey error/timeout ACCEPTS the token (defense-in-depth; availability wins).

Cases:
  * revoked jti                         -> 401 TOKEN_REVOKED
  * poisoned kid                        -> 401 TOKEN_REVOKED
  * agent epoch newer than token.iat    -> 401 TOKEN_REVOKED
  * agent epoch OLDER than token.iat    -> 200 (a token minted after the cascade survives)
  * clean token                         -> 200
  * Valkey raises (down)                -> 200 fail-open + skipped metric
  * Valkey times out (slow)             -> 200 fail-open + skipped metric
  * INTERNAL dual-mode: revoked FORWARDED agent (jti) -> 401 even though svc token is clean
  * INTERNAL dual-mode: revoked SERVICE token (jti)   -> 401
  * revocation_check_enabled=False      -> 200 even with a revoked jti present
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from guardrails_service.core import auth as auth_module
from guardrails_service.core import metrics
from guardrails_service.core.auth import Principal, require_principal
from guardrails_service.core.config import Settings
from guardrails_service.core.errors import install_exception_handlers

_KID = "test-key-1"
_SETTINGS = Settings()
ISSUER = _SETTINGS.auth_issuer_url
AUDIENCE = _SETTINGS.auth_platform_audience
PREFIX = _SETTINGS.revocation_key_prefix

TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"
JTI = "33333333-3333-3333-3333-333333333333"
SVC_JTI = "44444444-4444-4444-4444-444444444444"


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
        "jti": JTI,
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "scopes": ["guardrails:check"],
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
        "jti": SVC_JTI,
        "on_behalf_of": AGENT,
        "scopes": ["guardrails:check"],
    }
    claims.update(overrides)
    return claims


# ── Fake Valkey injected on app.state.valkey (matches ValkeyClient.get contract) ──
class _FakeValkey:
    """A minimal stand-in for ValkeyClient: a dict-backed ``get`` that can be made
    to raise (Valkey down) or hang past the timeout (Valkey slow)."""

    def __init__(
        self, data: dict[str, str] | None = None, *, raise_on_get: bool = False, hang: bool = False
    ) -> None:
        self.data = data or {}
        self.raise_on_get = raise_on_get
        self.hang = hang
        self.gets: list[str] = []

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        self.gets.append(key)
        if self.raise_on_get:
            raise ConnectionError("valkey down")
        if self.hang:
            # Mirror the real ValkeyClient.get: a slow backend bounded by ``timeout_seconds``
            # raises TimeoutError, which the verifier treats as "unavailable" and fails open.
            # ``hang_for`` sleeps well past the budget so the wait_for is what cancels us.
            async def _slow() -> str | None:
                await asyncio.sleep(5)
                return self.data.get(key)

            return await asyncio.wait_for(_slow(), timeout=timeout_seconds)
        return self.data.get(key)


# ── Tiny app exposing the real dependency + an injectable Valkey ──────────────────
def _make_client(valkey: _FakeValkey | None) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.state.valkey = valkey

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {"tenant_id": principal.tenant_id, "agent_id": principal.agent_id}

    return TestClient(app, raise_server_exceptions=False)


def _metric(outcome: str) -> float:
    return metrics.revocation_checks_total.labels(outcome=outcome)._value.get()


# ── revoked jti -> 401 ────────────────────────────────────────────────────────────
def test_revoked_jti_rejected() -> None:
    valkey = _FakeValkey({f"{PREFIX}jti:{JTI}": "1"})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── poisoned kid -> 401 ───────────────────────────────────────────────────────────
def test_poisoned_kid_rejected() -> None:
    valkey = _FakeValkey({f"{PREFIX}kid:{_KID}": "1"})
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── agent epoch newer than token.iat -> 401 (revoke-all cascade) ──────────────────
def test_agent_epoch_after_iat_rejected() -> None:
    now = int(time.time())
    valkey = _FakeValkey({f"{PREFIX}agent:{AGENT}": str(now + 100)})  # cascade AFTER mint
    client = _make_client(valkey)
    token = _mint(_agent_claims(iat=now, jti="no-such-jti"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── agent epoch OLDER than token.iat -> token survives (minted after the cascade) ──
def test_agent_epoch_before_iat_passes() -> None:
    now = int(time.time())
    valkey = _FakeValkey({f"{PREFIX}agent:{AGENT}": str(now - 100)})  # cascade BEFORE mint
    client = _make_client(valkey)
    token = _mint(_agent_claims(iat=now, jti="fresh-jti"))
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text


# ── clean token -> 200 + clean metric incremented ────────────────────────────────
def test_clean_token_passes_and_counts_clean() -> None:
    before = _metric("clean")
    valkey = _FakeValkey({})  # no revocation keys at all
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == AGENT
    assert _metric("clean") == before + 1


# ── Valkey down -> FAIL OPEN (accept) + skipped metric ────────────────────────────
def test_valkey_down_fails_open() -> None:
    before = _metric("skipped")
    valkey = _FakeValkey(raise_on_get=True)
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    assert _metric("skipped") == before + 1


# ── Valkey slow (past the budget) -> FAIL OPEN via timeout + skipped metric ───────
def test_valkey_timeout_fails_open() -> None:
    before = _metric("skipped")
    valkey = _FakeValkey(hang=True)
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    assert _metric("skipped") == before + 1


# ── No Valkey client wired at all -> FAIL OPEN (same posture as an outage) ─────────
def test_no_valkey_client_fails_open() -> None:
    client = _make_client(None)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text


# ── INTERNAL dual-mode: revoked FORWARDED agent must NOT slip through forwarding ───
def test_internal_revoked_forwarded_agent_rejected() -> None:
    # Service token is clean; the forwarded agent JWT's jti is revoked.
    valkey = _FakeValkey({f"{PREFIX}jti:{JTI}": "1"})
    client = _make_client(valkey)
    svc = _mint(_svc_claims(on_behalf_of=AGENT))
    agent = _mint(_agent_claims())  # jti == JTI (revoked)
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {svc}", "X-Forwarded-Agent-JWT": agent},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── INTERNAL dual-mode: revoked SERVICE token (its own jti) -> 401 ────────────────
def test_internal_revoked_service_token_rejected() -> None:
    valkey = _FakeValkey({f"{PREFIX}jti:{SVC_JTI}": "1"})
    client = _make_client(valkey)
    svc = _mint(_svc_claims(on_behalf_of=AGENT))  # svc jti == SVC_JTI (revoked)
    agent = _mint(_agent_claims())  # clean agent
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {svc}", "X-Forwarded-Agent-JWT": agent},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


# ── INTERNAL dual-mode: both clean -> 200 (service Principal) ─────────────────────
def test_internal_both_clean_passes() -> None:
    valkey = _FakeValkey({})
    client = _make_client(valkey)
    svc = _mint(_svc_claims(on_behalf_of=AGENT))
    agent = _mint(_agent_claims())
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {svc}", "X-Forwarded-Agent-JWT": agent},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == AGENT


# ── master enable flag OFF -> revocation lookup skipped entirely (token passes) ───
def test_revocation_check_disabled_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the cached Settings the dependency reads so the flag is OFF for this test.
    disabled = Settings(revocation_check_enabled=False)
    monkeypatch.setattr(auth_module, "get_settings", lambda: disabled)
    valkey = _FakeValkey({f"{PREFIX}jti:{JTI}": "1"})  # would otherwise reject
    client = _make_client(valkey)
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {_mint(_agent_claims())}"})
    assert resp.status_code == 200, resp.text
    assert valkey.gets == []  # the disabled flag short-circuits before any Valkey call
