"""WP07 — custom (tenant-authored) rules: ReDoS guard, quota, and EXECUTION.

Three layers:

* ``assert_regex_safe`` — the SAVE-time ReDoS guard rejects catastrophic backtrackers
  (``(a+)+$`` / ``(a|a)+$``) with ``UNSAFE_REGEX`` and accepts a safe bounded/anchored
  pattern. The guard runs the candidate in a hard-killable subprocess, so it ALWAYS returns
  within budget + a fixed spawn allowance.
* ``POST /v1/rules`` — the guard surfaces as 422 UNSAFE_REGEX; the per-tenant active-rule
  quota surfaces as 409 (and is fail-open when the limit is unresolved).
* The CRITICAL "custom rules execute" path — a created custom regex rule, once loaded via
  ``CustomRuleLoader.with_custom_rules`` and run through ``evaluate()``, actually FIRES.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from _fakedb import ScriptedPool
from guardrails_service.core.auth import Principal, require_principal
from guardrails_service.core.config import get_settings
from guardrails_service.main import create_app
from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import EffectivePolicy
from guardrails_service.services.rules import RULES_BY_ID, RuleContext
from guardrails_service.services.rules.custom import UNSAFE_REGEX, UnsafeRegexError, assert_regex_safe
from guardrails_service.services.rules.registry import CustomRuleLoader

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"


# ── ReDoS guard (pure, subprocess-backed) ───────────────────────────────────────


def test_redos_guard_rejects_nested_quantifier() -> None:
    with pytest.raises(UnsafeRegexError) as ei:
        assert_regex_safe("(a+)+$", max_length=1000, budget_ms=50.0)
    assert ei.value.reason == UNSAFE_REGEX


def test_redos_guard_rejects_alternation_overlap() -> None:
    with pytest.raises(UnsafeRegexError):
        assert_regex_safe("(a|a)+$", max_length=1000, budget_ms=50.0)


def test_redos_guard_rejects_oversize_pattern() -> None:
    with pytest.raises(UnsafeRegexError):
        assert_regex_safe("a" * 50, max_length=10, budget_ms=50.0)


def test_redos_guard_rejects_uncompilable_pattern() -> None:
    with pytest.raises(UnsafeRegexError):
        assert_regex_safe("(unterminated", max_length=1000, budget_ms=50.0)


def test_redos_guard_accepts_safe_anchored_pattern() -> None:
    compiled = assert_regex_safe(r"^SECRET-\d{4,8}$", max_length=1000, budget_ms=50.0)
    assert compiled.search("SECRET-1234")


def test_redos_guard_accepts_simple_keyword() -> None:
    compiled = assert_regex_safe(r"forbidden-token", max_length=1000, budget_ms=50.0)
    assert compiled.search("a forbidden-token here")


# ── API: create custom rule (422 / 201 / 409) ───────────────────────────────────


def _principal(scopes: list[str], claims: dict[str, Any] | None = None) -> Principal:
    return Principal(
        tenant_id=TENANT,
        agent_id=AGENT,
        scopes=scopes,
        principal_type="service",
        raw_claims=claims or {},
    )


def _admin() -> Principal:
    return _principal(["guardrails:check", "tenant:admin"])


@pytest_asyncio.fixture
async def build_client() -> AsyncIterator[Any]:  # type: ignore[misc]
    managers: list[Any] = []

    async def build(pool: ScriptedPool | None, principal_factory: Any = _admin) -> AsyncClient:
        app = create_app()
        app.dependency_overrides[require_principal] = principal_factory
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append((lm, app))
        app.state.db_pool = pool
        # A loader bound to the scripted pool so create -> invalidate path is exercised.
        app.state.custom_rule_loader = CustomRuleLoader(pool, ttl_seconds=30.0)  # type: ignore[arg-type]
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        managers.append((ac, None))
        return ac

    yield build

    for obj, _ in reversed(managers):
        if isinstance(obj, AsyncClient):
            await obj.aclose()
        else:
            await obj.__aexit__(None, None, None)


def _insert_responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
    # The default quota (custom_rules_max=100) > 0, so the COUNT(*) probe runs first.
    if "SELECT COUNT(*) FROM guardrails.rules" in query:
        return [(0,)]  # zero active rules -> under quota
    # The created-row RETURNING tuple in _SELECT_COLS order (rules.py _row_to_model).
    if "INSERT INTO guardrails.rules" in query and "RETURNING" in query:
        return [
            (
                params[0],  # rule_id (version_rule_id)
                params[1],  # root_rule_id
                TENANT,     # tenant_id::text
                "1",        # version
                "Block Secrets",  # name
                "regex",    # custom_type
                "input",    # direction
                "security", # default_category
                "high",     # default_severity
                "block",    # default_action
                "closed",   # default_fail_mode
                10,         # timeout_ms
                "active",   # status
                r"SECRET-\d{4}",  # pattern
                None,       # classifier_category
                None,       # threshold
            )
        ]
    return None


async def test_create_regex_rule_redos_rejected_422(build_client: Any) -> None:
    pool = ScriptedPool(_insert_responder)
    ac = await build_client(pool)
    resp = await ac.post(
        "/v1/rules",
        json={
            "name": "Evil",
            "type": "regex",
            "category": "security",
            "pattern": "(a+)+$",
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == UNSAFE_REGEX
    # The unsafe rule never reached the INSERT.
    assert not pool.ran("INSERT INTO guardrails.rules")


async def test_create_safe_regex_rule_201(build_client: Any) -> None:
    pool = ScriptedPool(_insert_responder)
    ac = await build_client(pool)
    resp = await ac.post(
        "/v1/rules",
        json={
            "name": "Block Secrets",
            "type": "regex",
            "category": "security",
            "severity": "high",
            "pattern": r"SECRET-\d{4}",
        },
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()["rule"]
    assert rule["type"] == "regex"
    assert rule["tenant_id"] == TENANT
    assert rule["status"] == "active"
    # Persisted under the JWT tenant (params include the tenant id from the principal).
    inserts = pool.find("INSERT INTO guardrails.rules")
    assert inserts
    assert TENANT in inserts[0][1]


async def test_create_rule_requires_write_scope_403(build_client: Any) -> None:
    pool = ScriptedPool(_insert_responder)
    ac = await build_client(pool, principal_factory=lambda: _principal(["guardrails:check"]))
    resp = await ac.post(
        "/v1/rules",
        json={"name": "X", "type": "regex", "category": "security", "pattern": "abc"},
    )
    assert resp.status_code == 403, resp.text


async def test_create_rule_without_pool_503(build_client: Any) -> None:
    ac = await build_client(None)
    resp = await ac.post(
        "/v1/rules",
        json={"name": "X", "type": "regex", "category": "security", "pattern": "abc"},
    )
    assert resp.status_code == 503, resp.text


async def test_create_rule_quota_exceeded_409(build_client: Any) -> None:
    # The quota COUNT(*) returns a value >= the claim limit -> 409.
    def _responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        if "SELECT COUNT(*) FROM guardrails.rules" in query:
            return [(3,)]  # 3 active rules already
        return _insert_responder(query, params)

    pool = ScriptedPool(_responder)
    # Claim limit of 3 -> active(3) >= limit(3) -> over quota.
    ac = await build_client(
        pool, principal_factory=lambda: _principal(
            ["guardrails:check", "tenant:admin"], {"limits": {"custom_rules_max": 3}}
        )
    )
    resp = await ac.post(
        "/v1/rules",
        json={"name": "X", "type": "regex", "category": "security", "pattern": "abc"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "custom_rules_max"


async def test_create_rule_quota_fail_open_when_unlimited(build_client: Any) -> None:
    # A claim limit of 0 means UNCAPPED: the quota COUNT must be skipped entirely.
    def _responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        if "SELECT COUNT(*) FROM guardrails.rules" in query:
            raise AssertionError("quota count must be skipped when limit <= 0")
        return _insert_responder(query, params)

    pool = ScriptedPool(_responder)
    ac = await build_client(
        pool, principal_factory=lambda: _principal(
            ["guardrails:check", "tenant:admin"], {"limits": {"custom_rules_max": 0}}
        )
    )
    resp = await ac.post(
        "/v1/rules",
        json={"name": "Block Secrets", "type": "regex", "category": "security",
              "pattern": r"SECRET-\d{4}"},
    )
    assert resp.status_code == 201, resp.text


async def test_classifier_threshold_rule_requires_fields_422(build_client: Any) -> None:
    pool = ScriptedPool(_insert_responder)
    ac = await build_client(pool)
    resp = await ac.post(
        "/v1/rules",
        json={"name": "T", "type": "classifier-threshold", "category": "toxicity"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "missing_threshold"


# ── CRITICAL: a custom rule, once loaded, actually EXECUTES in the pipeline ───────


def _custom_rule_rows(query: str, params: Any) -> list[tuple[Any, ...]] | None:
    """CustomRuleLoader._fetch row order (registry._fetch SELECT)."""
    if "FROM guardrails.rules" in query and "custom_type IS NOT NULL" in query:
        return [
            (
                "custom-abc:v1",   # rule_id
                TENANT,            # tenant_id::text
                1,                 # version
                "Block Secrets",   # name
                "regex",           # custom_type
                "input",           # direction
                "block",           # default_action
                "closed",          # default_fail_mode
                "high",            # default_severity
                "security",        # default_category
                10,                # timeout_ms
                "active",          # status
                r"SECRET-\d{4}",   # pattern
                None,              # classifier_category
                None,              # threshold
            )
        ]
    return None


@pytest.fixture(autouse=True)
def _drop_custom_specs() -> Any:
    """Remove any custom specs the loader registered into the shared RULES_BY_ID."""
    builtin = set(RULES_BY_ID)
    yield
    for rid in list(RULES_BY_ID):
        if rid not in builtin:
            del RULES_BY_ID[rid]


async def test_custom_rule_loads_and_fires_block() -> None:
    pool = ScriptedPool(_custom_rule_rows)
    loader = CustomRuleLoader(pool, ttl_seconds=30.0)  # type: ignore[arg-type]

    base = EffectivePolicy(policy_id="p", name="P", rules=())
    merged = await loader.with_custom_rules(base, TENANT)

    # The loader registered the custom spec into the live registry...
    assert "custom-abc:v1" in RULES_BY_ID
    # ...and appended its rule_id to the effective policy.
    merged_ids = {er.rule_id for er in merged.rules}
    assert "custom-abc:v1" in merged_ids

    # Run the merged policy through the UNMODIFIED pipeline: the custom rule FIRES + blocks.
    result = evaluate(
        text="my code is SECRET-9999 do not share",
        policy=merged,
        direction="input",
        tenant_id=TENANT,
        redaction_key="k",
        ctx=RuleContext(),
    )
    assert result.decision == "block"
    fired = {v.rule_id for v in result.violations}
    assert "custom-abc:v1" in fired


async def test_custom_rule_no_match_allows() -> None:
    pool = ScriptedPool(_custom_rule_rows)
    loader = CustomRuleLoader(pool, ttl_seconds=30.0)  # type: ignore[arg-type]
    merged = await loader.with_custom_rules(
        EffectivePolicy(policy_id="p", name="P", rules=()), TENANT
    )
    result = evaluate(
        text="nothing sensitive here",
        policy=merged,
        direction="input",
        tenant_id=TENANT,
        redaction_key="k",
        ctx=RuleContext(),
    )
    assert result.decision == "allow"


async def test_custom_rule_loader_no_pool_is_passthrough() -> None:
    loader = CustomRuleLoader(None, ttl_seconds=30.0)
    base = EffectivePolicy(policy_id="p", name="P", rules=())
    merged = await loader.with_custom_rules(base, TENANT)
    assert merged is base  # clean no-op passthrough


async def test_custom_rule_loader_caches_within_ttl() -> None:
    pool = ScriptedPool(_custom_rule_rows)
    loader = CustomRuleLoader(pool, ttl_seconds=300.0)  # type: ignore[arg-type]
    await loader.load_for_tenant(TENANT)
    first = pool.connections
    await loader.load_for_tenant(TENANT)  # served from cache, no new connection
    assert pool.connections == first
    loader.invalidate(TENANT)
    await loader.load_for_tenant(TENANT)  # re-reads after invalidation
    assert pool.connections > first


async def test_custom_rule_loader_fails_soft_on_db_error() -> None:
    from _fakedb import FailingPool

    loader = CustomRuleLoader(FailingPool(), ttl_seconds=30.0)  # type: ignore[arg-type]
    # A DB error must NEVER raise on the check path — the tenant simply has no custom rules.
    rule_ids = await loader.load_for_tenant(TENANT)
    assert rule_ids == ()


def test_custom_rules_default_quota_is_configured() -> None:
    # Sanity: the configured fallback quota is a positive cap (used when no claim present).
    assert get_settings().custom_rules_max > 0
