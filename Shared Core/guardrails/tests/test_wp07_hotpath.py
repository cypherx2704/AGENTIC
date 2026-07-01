"""WP07 — hot-path hardening: rate limiter, policy cache, persistence queue, detoxify fallback.

These are infra-bound features (Valkey / DB / the ML model) tested at the LOGIC level with
fakes so they stay deterministic and offline:

* :class:`RateLimiter` — DISABLED (no Valkey / flag off) => allow; configured + backend error
  => FAIL-CLOSED (unless fail_open); a count over the cap => deny on the right dimension.
* :class:`PolicyCache` — a miss / no-Valkey => ``None`` (the caller falls open to a live
  resolve); a round-trip put->get returns the policy.
* :class:`PersistenceQueue` — a no-pool enqueue is a no-op; with a pool, enqueued writes
  drain to the (fake) pool by the time :meth:`stop` returns.
* :func:`build_classifier` — ``CLASSIFIER_MODE=detoxify`` with the dep absent falls back to
  the stub (no crash).
"""

from __future__ import annotations

from typing import Any

from _fakedb import ScriptedPool
from guardrails_service.core.config import Settings
from guardrails_service.db.outbox import CheckWrite, ViolationRow
from guardrails_service.db.persist_queue import PersistenceQueue
from guardrails_service.services.classifier import StubClassifier, build_classifier
from guardrails_service.services.policy_cache import (
    PolicyCache,
    RateLimiter,
    resolve_contract19_limits,
)
from guardrails_service.services.policy_engine import EffectivePolicy, EnabledRule

TENANT = "00000000-0000-0000-0000-0000000000aa"


# ── Fake Valkey (duck-typed: eval / get / set) ───────────────────────────────────


class _FakeValkey:
    def __init__(self, *, eval_result: Any = None, eval_error: Exception | None = None) -> None:
        self._eval_result = eval_result
        self._eval_error = eval_error
        self._store: dict[str, str] = {}

    async def eval(self, script: str, *, keys: Any, args: Any, timeout_seconds: Any) -> object:
        if self._eval_error is not None:
            raise self._eval_error
        return self._eval_result

    async def get(self, key: str, *, timeout_seconds: Any = None) -> str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: Any = None, timeout_seconds: Any = None
    ) -> None:
        self._store[key] = value


# ── RateLimiter ──────────────────────────────────────────────────────────────────


async def test_rate_limiter_disabled_when_no_valkey() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True  # enabled, but no client wired => not configured
    limiter = RateLimiter(None, settings)
    assert limiter.configured is False
    result = await limiter.check(TENANT, 100)
    assert result.allowed is True


async def test_rate_limiter_disabled_when_flag_off() -> None:
    settings = Settings()  # rate_limit_enabled defaults False
    limiter = RateLimiter(_FakeValkey(eval_result=[0, 60]), settings)  # type: ignore[arg-type]
    assert limiter.configured is False
    assert (await limiter.check(TENANT, 100)).allowed is True


async def test_rate_limiter_allows_under_cap() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    limiter = RateLimiter(_FakeValkey(eval_result=[0, 60]), settings)  # type: ignore[arg-type]
    assert limiter.configured is True
    result = await limiter.check(TENANT, 100, checks_limit=10, bytes_limit=0)
    assert result.allowed is True


async def test_rate_limiter_denies_over_checks_cap() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    limiter = RateLimiter(_FakeValkey(eval_result=[1, 42]), settings)  # type: ignore[arg-type]
    result = await limiter.check(TENANT, 100, checks_limit=1, bytes_limit=0)
    assert result.allowed is False
    assert result.dimension == "checks"
    assert result.retry_after_seconds == 42


async def test_rate_limiter_denies_over_bytes_cap() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    limiter = RateLimiter(_FakeValkey(eval_result=[2, 30]), settings)  # type: ignore[arg-type]
    result = await limiter.check(TENANT, 999999, checks_limit=0, bytes_limit=1)
    assert result.allowed is False
    assert result.dimension == "bytes"


async def test_rate_limiter_fails_closed_on_backend_error() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    settings.rate_limit_fail_open = False
    limiter = RateLimiter(
        _FakeValkey(eval_error=RuntimeError("down")), settings  # type: ignore[arg-type]
    )
    result = await limiter.check(TENANT, 100, checks_limit=10, bytes_limit=10)
    assert result.allowed is False  # FAIL-CLOSED: a safety control rejects on backend error


async def test_rate_limiter_fail_open_override() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    settings.rate_limit_fail_open = True
    limiter = RateLimiter(
        _FakeValkey(eval_error=RuntimeError("down")), settings  # type: ignore[arg-type]
    )
    result = await limiter.check(TENANT, 100, checks_limit=10, bytes_limit=10)
    assert result.allowed is True  # operator override: availability over accounting


async def test_rate_limiter_uncapped_skips_roundtrip() -> None:
    settings = Settings()
    settings.rate_limit_enabled = True
    fake = _FakeValkey(eval_result=[1, 60])
    limiter = RateLimiter(fake, settings)  # type: ignore[arg-type]
    # Both dimensions uncapped (0) => nothing to enforce, immediate allow, no eval call.
    result = await limiter.check(TENANT, 100, checks_limit=0, bytes_limit=0)
    assert result.allowed is True


def test_resolve_contract19_limits_nested_and_flat() -> None:
    nested = resolve_contract19_limits({"limits": {"checks_per_min": 30, "input_bytes_per_min": 9}})
    assert nested == (30, 9)
    flat = resolve_contract19_limits({"checks_per_min": 7})
    assert flat == (7, None)
    assert resolve_contract19_limits({}) == (None, None)
    # bool must NOT be coerced to an int limit.
    assert resolve_contract19_limits({"checks_per_min": True}) == (None, None)


# ── PolicyCache (fail-open) ──────────────────────────────────────────────────────


async def test_policy_cache_disabled_returns_none() -> None:
    settings = Settings()
    cache = PolicyCache(None, settings)  # no Valkey
    assert cache.enabled is False
    assert await cache.get(TENANT, "agent") is None  # miss -> caller does a live resolve


async def test_policy_cache_miss_then_roundtrip() -> None:
    settings = Settings()
    cache = PolicyCache(_FakeValkey(), settings)  # type: ignore[arg-type]
    assert cache.enabled is True
    # Initial miss (nothing stored) -> None (fail-open to live resolve).
    assert await cache.get(TENANT, "agent") is None
    policy = EffectivePolicy(
        policy_id="p1", name="Cached", rules=(EnabledRule("pii-email-v1", "block"),)
    )
    await cache.put(TENANT, "agent", policy)
    got = await cache.get(TENANT, "agent")
    assert got is not None
    assert got.policy_id == "p1"
    assert got.name == "Cached"
    assert got.rules[0].rule_id == "pii-email-v1"
    assert got.rules[0].action_override == "block"


async def test_policy_cache_get_failopen_on_error() -> None:
    class _Boom:
        async def get(self, key: str, *, timeout_seconds: Any = None) -> str | None:
            raise RuntimeError("valkey down")

    settings = Settings()
    cache = PolicyCache(_Boom(), settings)  # type: ignore[arg-type]
    # A Valkey error is swallowed -> None (the caller falls open to a live resolve).
    assert await cache.get(TENANT, "agent") is None


# ── PersistenceQueue ─────────────────────────────────────────────────────────────


def _check_write() -> CheckWrite:
    return CheckWrite(
        check_id="11111111-1111-1111-1111-111111111111",
        request_id="req-1",
        tenant_id=TENANT,
        trace_id="trace-1",
        direction="input",
        decision="block",
        policy_id="policy-1",
        policy_name="Platform Default Policy",
        violations=[
            ViolationRow(
                rule_id="prompt-injection-v1",
                rule_name="Prompt Injection Detector",
                severity="critical",
                category="security",
                matched_text="ignore previous instruct",
                action="block",
            )
        ],
        input_bytes=10,
        rules_evaluated=6,
        duration_ms=5,
    )


async def test_persist_queue_noop_without_pool() -> None:
    queue = PersistenceQueue(lambda: None, producer_version="0.1.0")
    assert queue.enabled is False
    await queue.start()
    queue.enqueue(_check_write())  # no pool -> dropped silently (no-op)
    await queue.stop()
    # Nothing to assert beyond "did not raise"; there is no pool to have written to.


async def test_persist_queue_drains_to_pool_on_stop() -> None:
    pool = ScriptedPool()
    queue = PersistenceQueue(lambda: pool, producer_version="0.1.0")
    assert queue.enabled is True
    await queue.start()
    queue.enqueue(_check_write())
    # stop() drains the backlog (bounded wait) before cancelling the worker.
    await queue.stop()

    # The check's violation row + outbox events landed on the fake pool.
    assert pool.ran("INSERT INTO guardrails.violations")
    assert pool.ran("INSERT INTO guardrails.outbox")
    # The write was tenant-scoped via in_tenant (set_config app.tenant_id).
    set_cfg = [(q, p) for q, p in pool.executed if "set_config('app.tenant_id'" in q]
    assert set_cfg and set_cfg[0][1] == (TENANT,)


async def test_persist_queue_overflow_drops_new_item() -> None:
    pool = ScriptedPool()
    # maxsize 1; do NOT start the worker, so the queue cannot drain -> the 2nd enqueue drops.
    queue = PersistenceQueue(lambda: pool, producer_version="0.1.0", maxsize=1)
    queue.enqueue(_check_write())  # fills the single slot
    queue.enqueue(_check_write())  # overflow -> dropped (logged + counted), no raise
    # Drain manually to confirm exactly ONE item was retained.
    await queue.start()
    await queue.stop()
    violation_inserts = pool.find("INSERT INTO guardrails.violations")
    assert len(violation_inserts) == 1


# ── Detoxify graceful fallback ───────────────────────────────────────────────────


def test_detoxify_mode_falls_back_to_stub_when_absent() -> None:
    settings = Settings()
    settings.classifier_mode = "detoxify"
    classifier = build_classifier(settings)
    # The `detoxify`/torch extra is NOT installed in the test env, so the build must
    # gracefully substitute the always-ready stub rather than crash.
    assert isinstance(classifier, StubClassifier)
    assert classifier.ready is True


def test_stub_mode_builds_stub() -> None:
    settings = Settings()  # classifier_mode defaults to 'stub'
    assert isinstance(build_classifier(settings), StubClassifier)
