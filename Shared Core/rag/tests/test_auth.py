"""Dual-mode auth resolution + scope enforcement + revocation mirror (unit)."""

from __future__ import annotations

import pytest

from rag_service.core import auth
from rag_service.core.auth import (
    SCOPE_ADMIN,
    SCOPE_INTERNAL_READ,
    SCOPE_QUERY,
    Principal,
    _principal_from_service_claims,
    _resolve_principal,
    require_scope,
)
from rag_service.core.config import Settings
from rag_service.core.errors import ApiError


class _Req:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}


def _settings() -> Settings:
    return Settings()


def test_external_mode_resolves_agent(monkeypatch) -> None:  # noqa: ANN001
    claims = {
        "sub": "agent-1", "tenant_id": "t1", "agent_id": "a1",
        "scopes": ["rag:query"], "api_key_id": None,
    }
    monkeypatch.setattr(auth, "_decode", lambda token, settings: claims)
    req = _Req({"authorization": "Bearer agent.jwt"})
    principal, subjects = _resolve_principal(req, _settings())
    assert principal.tenant_id == "t1"
    assert principal.agent_id == "a1"
    assert principal.principal_type == "agent"
    assert len(subjects) == 1  # the bearer


def test_internal_mode_requires_matching_on_behalf_of(monkeypatch) -> None:  # noqa: ANN001
    svc = {"sub": "svc:rag", "tenant_id": "t1", "on_behalf_of": "a1", "scopes": ["rag:query"]}
    agent = {"sub": "agent-1", "tenant_id": "t1", "agent_id": "a1", "scopes": ["rag:query"]}

    def _decode(token, settings):  # noqa: ANN001
        return svc if token == "svc.jwt" else agent

    monkeypatch.setattr(auth, "_decode", _decode)
    req = _Req({"authorization": "Bearer svc.jwt", "x-forwarded-agent-jwt": "agent.jwt"})
    principal, subjects = _resolve_principal(req, _settings())
    assert principal.principal_type == "service"
    assert principal.agent_id == "a1"
    assert len(subjects) == 2  # service token + forwarded agent


def test_internal_mode_mismatch_rejected(monkeypatch) -> None:  # noqa: ANN001
    svc = {"sub": "svc:rag", "tenant_id": "t1", "on_behalf_of": "WRONG", "scopes": ["rag:query"]}
    agent = {"sub": "agent-1", "tenant_id": "t1", "agent_id": "a1", "scopes": ["rag:query"]}

    def _decode(token, settings):  # noqa: ANN001
        return svc if token == "svc.jwt" else agent

    monkeypatch.setattr(auth, "_decode", _decode)
    req = _Req({"authorization": "Bearer svc.jwt", "x-forwarded-agent-jwt": "agent.jwt"})
    with pytest.raises(ApiError) as exc:
        _resolve_principal(req, _settings())
    assert exc.value.code == "UNAUTHORIZED"


def test_cross_tenant_platform_read_service_token() -> None:
    # Skills -> RAG: a service token carrying the platform tenant + on_behalf_of (no forwarded JWT).
    claims = {
        "sub": "svc:skills-service", "tenant_id": "00000000-0000-0000-0000-000000000001",
        "on_behalf_of": "agent-in-T", "scopes": ["internal:read"],
    }
    principal = _principal_from_service_claims(claims)
    assert principal.tenant_id == "00000000-0000-0000-0000-000000000001"
    assert principal.on_behalf_of == "agent-in-T"
    assert SCOPE_INTERNAL_READ in principal.scopes


@pytest.mark.asyncio
async def test_require_scope_allows_when_held(monkeypatch) -> None:  # noqa: ANN001
    principal = Principal(tenant_id="t", agent_id="a", scopes=[SCOPE_ADMIN], principal_type="agent")
    monkeypatch.setattr(auth, "_resolve_principal", lambda req, s: (principal, []))

    async def _no_revoke(req, s, subj):  # noqa: ANN001
        return None

    monkeypatch.setattr(auth, "_enforce_revocation", _no_revoke)
    dep = require_scope(SCOPE_ADMIN)
    result = await dep(_Req({}))  # type: ignore[arg-type]
    assert result is principal


@pytest.mark.asyncio
async def test_require_scope_denies_when_missing(monkeypatch) -> None:  # noqa: ANN001
    principal = Principal(tenant_id="t", agent_id="a", scopes=[SCOPE_QUERY], principal_type="agent")
    monkeypatch.setattr(auth, "_resolve_principal", lambda req, s: (principal, []))

    async def _no_revoke(req, s, subj):  # noqa: ANN001
        return None

    monkeypatch.setattr(auth, "_enforce_revocation", _no_revoke)
    dep = require_scope(SCOPE_ADMIN)
    with pytest.raises(ApiError) as exc:
        await dep(_Req({}))  # type: ignore[arg-type]
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_internal_read_satisfies_query_scope(monkeypatch) -> None:  # noqa: ANN001
    principal = Principal(
        tenant_id="t", agent_id=None, scopes=[SCOPE_INTERNAL_READ], principal_type="service"
    )
    monkeypatch.setattr(auth, "_resolve_principal", lambda req, s: (principal, []))

    async def _no_revoke(req, s, subj):  # noqa: ANN001
        return None

    monkeypatch.setattr(auth, "_enforce_revocation", _no_revoke)
    dep = require_scope(SCOPE_QUERY)
    assert await dep(_Req({})) is principal  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_auth_header_401(app_client) -> None:  # noqa: ANN001
    # No auth monkeypatch here -> the real _bearer runs and rejects the missing header.
    resp = await app_client.post("/v1/kbs", json={"name": "x"})
    assert resp.status_code == 401
