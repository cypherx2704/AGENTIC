"""WP02 — submit-time validation: caller-vs-target rule + user_id claim extraction.

Caller-vs-target rule (amended fix #6, FIRST-CYCLE RULE): ``body.agent_id`` MUST equal
the verified JWT's ``agent_id``, checked BEFORE any agent load or persistence —
cross-agent invocation arrives only via 9B A2A delegation tokens (📋). These tests
drive the REAL ``POST /v1/tasks`` endpoint through the ASGI app with
``require_principal`` overridden (the same seam every app-level test uses):

  * mismatch                      -> 422 VALIDATION_ERROR (AGENT_ID_MISMATCH)
  * api_key-only (agent_id=None)  -> 422 VALIDATION_ERROR ('token carries no agent identity')
  * match                         -> passes the rule (proceeds to the pool check; with
                                     the conftest's nulled db_pool that is a 503 — i.e.
                                     validation did NOT reject it)

user_id semantics (amended minor): ``_user_id_from_claims`` reads ONLY an explicit
``user_id`` claim — the JWT-``sub`` fallback is removed (sub is the agent, not a user),
so a sub-only token yields ``user_id`` None (NULL on the task row).
"""

from __future__ import annotations

from agent_runtime.api.tasks import _user_id_from_claims
from agent_runtime.core.auth import Principal, require_principal

# Mirrors the conftest fixed Principal (tests/ is not an importable package).
TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
OTHER_AGENT = "00000000-0000-0000-0000-0000000000cc"


# ── caller-vs-target: mismatch -> 422 VALIDATION_ERROR ───────────────────────────────
async def test_agent_id_mismatch_rejected_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/v1/tasks",
        json={"agent_id": OTHER_AGENT, "input": {"message": "hi"}, "mode": "sync"},
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["reason"] == "AGENT_ID_MISMATCH"
    assert "delegation" in error["message"]


# ── caller-vs-target: api_key-only principal (no agent identity) -> 422 ─────────────
async def test_api_key_principal_without_agent_identity_rejected_422(client) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    api_key_principal = Principal(
        tenant_id=TEST_TENANT,
        agent_id=None,  # api_key-only callers may carry no agent identity
        scopes=["agent:execute"],
        principal_type="api_key",
        api_key_id="key-1",
        raw_token="test.api-key-jwt",
        raw_claims={"tenant_id": TEST_TENANT, "api_key_id": "key-1"},
    )
    app.dependency_overrides[require_principal] = lambda: api_key_principal

    resp = await client.post(
        "/v1/tasks",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}, "mode": "sync"},
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "token carries no agent identity" in error["message"]
    assert error["details"]["reason"] == "NO_AGENT_IDENTITY"


# ── caller-vs-target: match passes the rule (checked BEFORE persistence) ─────────────
async def test_matching_agent_id_passes_caller_target_rule(client) -> None:  # type: ignore[no-untyped-def]
    # The conftest principal's agent_id == TEST_AGENT, so the rule passes and the
    # request proceeds to the task-store check. With the fixture's nulled db_pool that
    # surfaces as 503 SERVICE_UNAVAILABLE — i.e. NOT a 422 from the rule.
    resp = await client.post(
        "/v1/tasks",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}, "mode": "sync"},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── user_id: explicit claim ONLY — the JWT-sub fallback is removed ───────────────────
def _principal_with_claims(claims: dict) -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["agent:execute"],
        raw_token="t",
        raw_claims=claims,
    )


def test_user_id_sub_only_token_yields_none() -> None:
    principal = _principal_with_claims({"sub": TEST_AGENT, "tenant_id": TEST_TENANT})
    assert _user_id_from_claims(principal) is None


def test_user_id_explicit_claim_is_used() -> None:
    principal = _principal_with_claims(
        {"sub": TEST_AGENT, "user_id": "33333333-3333-3333-3333-333333333333"}
    )
    assert _user_id_from_claims(principal) == "33333333-3333-3333-3333-333333333333"


def test_user_id_missing_claims_yields_none() -> None:
    assert _user_id_from_claims(_principal_with_claims({})) is None
