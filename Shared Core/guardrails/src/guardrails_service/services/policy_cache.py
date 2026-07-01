"""Hot-path Valkey helpers: policy cache + per-tenant rate limiter (WP07).

Two independent hot-path concerns share this module because both are thin, fail-soft
wrappers over the lazy :class:`~guardrails_service.core.valkey.ValkeyClient`:

* :class:`PolicyCache` — caches the resolved :class:`EffectivePolicy` per (tenant, agent)
  for ``policy_cache_ttl_seconds``. It is purely a latency optimisation: every miss /
  serialisation problem / Valkey error FALLS OPEN to a live ``policy_engine.resolve`` so
  the decision never depends on the cache being up. The policy is serialised to a compact
  JSON form (policy_id, name, rules) — never raw text.

* :class:`RateLimiter` — per-tenant checks-per-minute + input-bytes-per-minute quota
  (Auth/Contract-19). It is a SAFETY control, so when ENABLED and the backend ERRORS the
  default posture is FAIL-CLOSED (reject, 429). Crucially:
    - "no limiter configured" (no Valkey client wired) => DISABLED (the limiter is a
      no-op), so the existing check tests + keyless local dev stay green; and
    - "limiter configured but the backend errors" => FAIL-CLOSED, unless an operator sets
      ``rate_limit_fail_open=true`` (documented availability-over-accounting override).
  The window counter is incremented atomically with a single Lua script (INCRBY + EXPIRE
  on first touch) so two concurrent checks cannot both slip past the cap.

Both degrade to a no-op when no Valkey client is present on ``app.state`` — the unit/test
app sets ``app.state.db_pool = None`` and wires no Valkey, so neither feature engages.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import structlog

from ..core import metrics
from ..core.config import Settings
from ..core.valkey import ValkeyClient
from .policy_engine import EffectivePolicy, EnabledRule

logger = structlog.get_logger(__name__)


# ── Policy cache ─────────────────────────────────────────────────────────────────
class PolicyCache:
    """Valkey-backed cache of the resolved EffectivePolicy per (tenant, agent).

    Fail-OPEN by construction: :meth:`get` returns ``None`` (a miss) on any Valkey error
    or malformed payload, and :meth:`put` swallows write errors. The caller always has a
    live resolve to fall back to.
    """

    def __init__(self, valkey: ValkeyClient | None, settings: Settings) -> None:
        self._valkey = valkey
        self._ttl = settings.policy_cache_ttl_seconds
        self._prefix = settings.policy_cache_key_prefix
        self._timeout = settings.policy_cache_valkey_timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._valkey is not None and self._ttl > 0

    def _key(self, tenant_id: str, agent_id: str | None) -> str:
        return f"{self._prefix}{tenant_id}:{agent_id or '-'}"

    async def get(self, tenant_id: str, agent_id: str | None) -> EffectivePolicy | None:
        if not self.enabled:
            metrics.policy_cache_total.labels("disabled").inc()
            return None
        assert self._valkey is not None
        try:
            raw = await self._valkey.get(self._key(tenant_id, agent_id), timeout_seconds=self._timeout)
        except Exception as exc:  # noqa: BLE001 — cache is best-effort; fall open to a live resolve
            metrics.policy_cache_total.labels("error").inc()
            logger.warning("policy_cache_get_failed", error=str(exc))
            return None
        if raw is None:
            metrics.policy_cache_total.labels("miss").inc()
            return None
        policy = _deserialize_policy(raw)
        if policy is None:
            metrics.policy_cache_total.labels("error").inc()
            return None
        metrics.policy_cache_total.labels("hit").inc()
        return policy

    async def put(self, tenant_id: str, agent_id: str | None, policy: EffectivePolicy) -> None:
        if not self.enabled:
            return
        assert self._valkey is not None
        try:
            await self._valkey.set(
                self._key(tenant_id, agent_id),
                _serialize_policy(policy),
                ttl_seconds=self._ttl,
                timeout_seconds=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the check on a cache write
            logger.warning("policy_cache_put_failed", error=str(exc))


def _serialize_policy(policy: EffectivePolicy) -> str:
    return json.dumps(
        {
            "policy_id": policy.policy_id,
            "name": policy.name,
            "rules": [[r.rule_id, r.action_override] for r in policy.rules],
        },
        separators=(",", ":"),
    )


def _deserialize_policy(raw: str) -> EffectivePolicy | None:
    try:
        data = json.loads(raw)
        rules = tuple(
            EnabledRule(str(rid), override if isinstance(override, str) else None)
            for rid, override in data["rules"]
        )
        return EffectivePolicy(str(data["policy_id"]), str(data["name"]), rules)
    except Exception as exc:  # noqa: BLE001 — a malformed cache entry is treated as a miss
        logger.warning("policy_cache_deserialize_failed", error=str(exc))
        return None


# ── Rate limiter ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    # Which dimension tripped ('checks' | 'bytes'); None when allowed/failopen.
    dimension: str | None = None
    # Seconds the caller should wait before retrying (Retry-After), best-effort.
    retry_after_seconds: int = 60


# Atomic fixed-window limiter. One round-trip increments BOTH dimensions and reports the
# first that exceeds its cap. A cap of 0 (or negative) means "uncapped" for that dimension.
# KEYS[1]=checks counter, KEYS[2]=bytes counter; ARGV: checks_limit, bytes_cost,
# bytes_limit, window_seconds. Returns {tripped_dimension_code, ttl}: 0=ok, 1=checks, 2=bytes.
_LIMITER_LUA = """
local checks_limit = tonumber(ARGV[1])
local bytes_cost   = tonumber(ARGV[2])
local bytes_limit  = tonumber(ARGV[3])
local window       = tonumber(ARGV[4])

local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], window) end

local b = redis.call('INCRBY', KEYS[2], bytes_cost)
if b == bytes_cost then redis.call('EXPIRE', KEYS[2], window) end

local ttl = redis.call('TTL', KEYS[1])
if ttl == nil or ttl < 0 then ttl = window end

if checks_limit > 0 and c > checks_limit then
  return {1, ttl}
end
if bytes_limit > 0 and b > bytes_limit then
  return {2, ttl}
end
return {0, ttl}
"""

_DIMENSION_BY_CODE = {1: "checks", 2: "bytes"}


class RateLimiter:
    """Per-tenant checks/min + bytes/min limiter (Auth/Contract-19). FAIL-CLOSED."""

    def __init__(self, valkey: ValkeyClient | None, settings: Settings) -> None:
        self._valkey = valkey
        self._enabled = settings.rate_limit_enabled
        self._fail_open = settings.rate_limit_fail_open
        self._prefix = settings.rate_limit_key_prefix
        self._timeout = settings.rate_limit_valkey_timeout_seconds
        self._default_checks = settings.rate_limit_default_checks_per_min
        self._default_bytes = settings.rate_limit_default_input_bytes_per_min
        self._window_seconds = 60

    @property
    def configured(self) -> bool:
        """True only when the feature is ENABLED *and* a Valkey client is wired.

        "Not configured" => the limiter is a no-op (allows everything). This keeps unit
        tests / keyless local dev green: those run with ``rate_limit_enabled`` OFF and no
        Valkey client, so :meth:`check` short-circuits to allow before any backend call.
        """
        return self._enabled and self._valkey is not None

    async def check(
        self,
        tenant_id: str,
        input_bytes: int,
        *,
        checks_limit: int | None = None,
        bytes_limit: int | None = None,
    ) -> RateLimitResult:
        """Atomically account one check + ``input_bytes`` against the tenant's window.

        ``checks_limit`` / ``bytes_limit`` come from the caller's Auth/Contract-19 claims
        when present; ``None`` falls back to the configured defaults. When the limiter is
        not configured this is an immediate allow. When configured and the backend errors
        the result is FAIL-CLOSED (reject) unless ``rate_limit_fail_open`` is set.
        """
        if not self.configured:
            metrics.rate_limit_decisions_total.labels("disabled", "none").inc()
            return RateLimitResult(allowed=True)
        assert self._valkey is not None

        c_limit = self._default_checks if checks_limit is None else checks_limit
        b_limit = self._default_bytes if bytes_limit is None else bytes_limit
        # Both dimensions uncapped => nothing to enforce; skip the round-trip.
        if c_limit <= 0 and b_limit <= 0:
            metrics.rate_limit_decisions_total.labels("disabled", "none").inc()
            return RateLimitResult(allowed=True)

        window = self._window_seconds
        base = f"{self._prefix}{tenant_id}:{self._window_bucket()}"
        try:
            raw = await self._valkey.eval(
                _LIMITER_LUA,
                keys=[f"{base}:c", f"{base}:b"],
                args=[c_limit, max(input_bytes, 0), b_limit, window],
                timeout_seconds=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — backend down/slow: apply the fail posture
            if self._fail_open:
                metrics.rate_limit_decisions_total.labels("failopen", "none").inc()
                logger.warning("rate_limit_fail_open", reason="valkey_error", error=str(exc))
                return RateLimitResult(allowed=True)
            metrics.rate_limit_decisions_total.labels("limited", "none").inc()
            logger.warning("rate_limit_fail_closed", reason="valkey_error", error=str(exc))
            return RateLimitResult(allowed=False, dimension=None, retry_after_seconds=window)

        code, ttl = _parse_limiter_reply(raw, fallback_ttl=window)
        if code == 0:
            metrics.rate_limit_decisions_total.labels("allowed", "none").inc()
            return RateLimitResult(allowed=True)
        dimension = _DIMENSION_BY_CODE.get(code, "checks")
        metrics.rate_limit_decisions_total.labels("limited", dimension).inc()
        return RateLimitResult(allowed=False, dimension=dimension, retry_after_seconds=ttl)

    def _window_bucket(self) -> int:
        # Fixed-window bucket id (minute-aligned). Keeping it in the key (rather than relying
        # only on EXPIRE) makes the window deterministic and lets old buckets expire cleanly.
        import time

        return int(time.time() // self._window_seconds)


def _parse_limiter_reply(raw: object, *, fallback_ttl: int) -> tuple[int, int]:
    """Coerce the Lua ``{code, ttl}`` reply into ints (redis-py returns a list)."""
    try:
        code = int(raw[0])  # type: ignore[index]
        ttl_val = int(raw[1])  # type: ignore[index]
        ttl = ttl_val if ttl_val > 0 else fallback_ttl
        return code, ttl
    except Exception:  # noqa: BLE001 — unexpected shape: treat as allow (we already round-tripped)
        return 0, fallback_ttl


def resolve_contract19_limits(raw_claims: dict[str, object]) -> tuple[int | None, int | None]:
    """Pull per-tenant checks/min + input-bytes/min limits from JWT claims (Contract-19).

    Auth mints the tenant's plan limits onto the token. We accept either a nested
    ``limits`` object or flat ``checks_per_min`` / ``input_bytes_per_min`` claims. Returns
    ``(checks_limit, bytes_limit)`` with ``None`` for any dimension the token does not
    carry (the limiter then uses the configured default).
    """
    src: dict[str, object] = raw_claims
    nested = raw_claims.get("limits")
    if isinstance(nested, dict):
        src = {**raw_claims, **nested}
    return _int_claim(src.get("checks_per_min")), _int_claim(src.get("input_bytes_per_min"))


def _int_claim(value: object) -> int | None:
    if isinstance(value, bool):  # bool is a subclass of int — exclude it explicitly
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and not math.isnan(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
