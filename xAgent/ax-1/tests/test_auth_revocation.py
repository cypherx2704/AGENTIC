"""WP03: verifier-side live-revocation mirror in xAgent's inbound auth path.

Unit-tests ``_enforce_revocation`` directly (no JWKS/live server needed): a fake Valkey
returns a :class:`RevocationState`, and we assert the shared kill-switch semantics —
reject 401 TOKEN_REVOKED on a jti/kid/agent hit, pass when clean, and FAIL OPEN when
Valkey is unavailable (revocation is defense-in-depth; availability wins).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent_runtime.core.auth import Principal, _enforce_revocation
from agent_runtime.core.config import get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.services.valkey import RevocationState

_SETTINGS = get_settings()


class _FakeValkey:
    """Stand-in for ValkeyClient.revocation_lookup: returns a fixed state or raises."""

    def __init__(self, state: RevocationState | None = None, raise_exc: Exception | None = None) -> None:
        self._state = state
        self._raise = raise_exc
        self.calls = 0

    async def revocation_lookup(self, **_kwargs: object) -> RevocationState:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        assert self._state is not None
        return self._state


def _request(valkey: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(valkey=valkey)))


def _principal(
    *, iat: int | None = None, agent_id: str | None = "agent-1", kid: str | None = "kid-1"
) -> Principal:
    iat = iat if iat is not None else int(time.time())
    return Principal(
        tenant_id="11111111-1111-1111-1111-1111111111aa",
        agent_id=agent_id,
        scopes=["agent:execute"],
        raw_token="tok",
        raw_claims={"jti": "22222222-2222-2222-2222-222222222222", "iat": iat, "agent_id": agent_id},
        kid=kid,
    )


async def test_clean_token_passes() -> None:
    vk = _FakeValkey(RevocationState(jti_revoked=False, kid_revoked=False, agent_epoch=None))
    await _enforce_revocation(_request(vk), _SETTINGS, _principal())  # no raise
    assert vk.calls == 1


async def test_revoked_jti_rejected() -> None:
    vk = _FakeValkey(RevocationState(jti_revoked=True, kid_revoked=False, agent_epoch=None))
    with pytest.raises(ApiError) as ei:
        await _enforce_revocation(_request(vk), _SETTINGS, _principal())
    assert ei.value.code == ErrorCode.TOKEN_REVOKED


async def test_poisoned_kid_rejected() -> None:
    vk = _FakeValkey(RevocationState(jti_revoked=False, kid_revoked=True, agent_epoch=None))
    with pytest.raises(ApiError) as ei:
        await _enforce_revocation(_request(vk), _SETTINGS, _principal())
    assert ei.value.code == ErrorCode.TOKEN_REVOKED


async def test_agent_epoch_newer_than_iat_rejected() -> None:
    now = int(time.time())
    vk = _FakeValkey(RevocationState(False, False, agent_epoch=now + 100))
    with pytest.raises(ApiError) as ei:
        await _enforce_revocation(_request(vk), _SETTINGS, _principal(iat=now))
    assert ei.value.code == ErrorCode.TOKEN_REVOKED


async def test_agent_epoch_older_than_iat_passes() -> None:
    now = int(time.time())
    vk = _FakeValkey(RevocationState(False, False, agent_epoch=now - 100))
    await _enforce_revocation(_request(vk), _SETTINGS, _principal(iat=now))  # token predates cutoff -> ok


async def test_valkey_error_fails_open() -> None:
    vk = _FakeValkey(raise_exc=RuntimeError("valkey unavailable"))
    await _enforce_revocation(_request(vk), _SETTINGS, _principal())  # no raise (fail-open)
    assert vk.calls == 1


async def test_no_valkey_client_fails_open() -> None:
    await _enforce_revocation(_request(None), _SETTINGS, _principal())  # no raise


async def test_disabled_flag_skips_lookup() -> None:
    vk = _FakeValkey(RevocationState(jti_revoked=True, kid_revoked=True, agent_epoch=None))
    disabled = _SETTINGS.model_copy(update={"revocation_check_enabled": False})
    await _enforce_revocation(_request(vk), disabled, _principal())  # not rejected despite revoked state
    assert vk.calls == 0
