"""Per-tenant rate limiting (WP05) — fixed-window requests + post-hoc token debit.

Two pieces the chat path calls around a completion:

* :func:`enforce_pre` — BEFORE the provider call. Atomic ``INCR`` + ``EXPIRE`` on a
  60s fixed-window request counter; if it exceeds ``requests_per_min`` -> raise an
  ``ApiError`` (429 ``RATE_LIMIT_EXCEEDED``) with a ``Retry-After`` header (seconds to
  window end). It ALSO checks the per-minute token counters that were debited by
  PRIOR requests in the same window: if the tenant is already over
  ``prompt_tokens_per_min`` / ``completion_tokens_per_min`` it rejects here too
  (post-hoc debit, pre-check enforcement — a heavy minute throttles the *next*
  request, matching the plan's "post-hoc tokens_per_min debit").

* :func:`debit_tokens` — AFTER a completion. Adds the consumed prompt/completion
  tokens to the per-minute counters (``INCRBY`` + ``EXPIRE``). Never raises; never
  rejects (enforcement happens on the next request's :func:`enforce_pre`).

FAIL-OPEN: any Valkey problem (no client wired, connect error, timeout) -> ALLOW the
request / skip the debit, log ``rate_limit_failopen=true``, and bump
``rate_limit_failopen_total``. Availability wins (same posture as the WP03 revocation
mirror). ``settings.rate_limit_enabled = false`` makes both functions no-ops.

Keys (all under ``settings.rate_limit_key_prefix``, default ``cypherx:llms:rl:``):
    req:{tenant}:{windowMin}     request count this window
    ptok:{tenant}:{windowMin}    prompt tokens debited this window
    ctok:{tenant}:{windowMin}    completion tokens debited this window
where ``windowMin = floor(now / window_seconds)`` (default 60s windows).

Public API (call these from the chat path):

    await enforce_pre(valkey, principal, limits, settings=...)   # raises 429 over limit
    await debit_tokens(valkey, principal, prompt_tokens, completion_tokens, settings=...)

``valkey`` is ``request.app.state.valkey`` (may be ``None``). ``limits`` is the
:class:`~llms_gateway.services.auth_client.PlanLimits` from ``resolve_limits``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..core.auth import Principal
    from ..db.valkey import ValkeyClient
    from .auth_client import PlanLimits

logger = structlog.get_logger(__name__)


def _window_id(window_seconds: int) -> int:
    """Current fixed-window index: ``floor(epoch_seconds / window_seconds)``."""
    return int(time.time()) // window_seconds


def _seconds_to_window_end(window_seconds: int) -> int:
    """Whole seconds remaining until the current window rolls over (>= 1)."""
    now = time.time()
    end = (int(now) // window_seconds + 1) * window_seconds
    return max(1, int(end - now))


def _keys(prefix: str, tenant_id: str, window: int) -> tuple[str, str, str]:
    """(request, prompt-token, completion-token) counter keys for the window."""
    return (
        f"{prefix}req:{tenant_id}:{window}",
        f"{prefix}ptok:{tenant_id}:{window}",
        f"{prefix}ctok:{tenant_id}:{window}",
    )


def _reject(dimension: str, retry_after: int, limit: int) -> None:
    """Raise the Contract-2 429 with ``Retry-After`` + the ``X-RateLimit-*`` headers.

    Per ``contracts/http/headers.md`` (and Contract-15 case 14) a 429 carries the full
    rate-limit header set: ``Limit`` = window quota, ``Remaining`` = 0 on breach,
    ``Reset`` = epoch seconds at window roll-over, ``Resource`` = the breached dimension.
    """
    metrics.rate_limit_rejected_total.labels(dimension).inc()
    reset_epoch = int(time.time()) + retry_after
    raise ApiError(
        ErrorCode.RATE_LIMIT_EXCEEDED,
        f"Rate limit exceeded for {dimension} (limit {limit} per minute).",
        status_code=429,
        details={"dimension": dimension, "limit": limit, "retry_after_seconds": retry_after},
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_epoch),
            "X-RateLimit-Resource": dimension,
        },
    )


async def enforce_pre(
    valkey: ValkeyClient | None,
    principal: Principal,
    limits: PlanLimits,
    *,
    settings: Settings | None = None,
) -> None:
    """Pre-request rate-limit gate. Raises 429 ``RATE_LIMIT_EXCEEDED`` when over limit.

    Increments the request counter for the current 60s window (atomic INCR+EXPIRE) and
    rejects if it exceeds ``limits.requests_per_min``. Also rejects (without
    incrementing) when the tenant has already blown the token budget for this window
    via prior :func:`debit_tokens` calls. FAIL-OPEN on any Valkey error.

    Args:
        valkey: ``request.app.state.valkey`` (or ``None`` -> fail open / allow).
        principal: the authenticated caller (``tenant_id`` keys the counters).
        limits: resolved :class:`PlanLimits` for the tenant's plan.
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.rate_limit_enabled:
        return
    if valkey is None:
        _failopen("enforce_pre", "no_valkey_client", principal)
        return

    window_seconds = settings.rate_limit_window_seconds
    window = _window_id(window_seconds)
    req_key, ptok_key, ctok_key = _keys(settings.rate_limit_key_prefix, principal.tenant_id, window)
    timeout = settings.rate_limit_valkey_timeout_seconds

    try:
        # Token enforcement is a READ of counters debited by PRIOR requests this window.
        # Check BEFORE incrementing the request counter so a token-blocked tenant does
        # not also consume request budget.
        prompt_used = _to_int(
            await valkey.get(ptok_key, timeout_seconds=timeout)
        )
        if limits.prompt_tokens_per_min > 0 and prompt_used >= limits.prompt_tokens_per_min:
            _reject("prompt_tokens", _seconds_to_window_end(window_seconds), limits.prompt_tokens_per_min)
        completion_used = _to_int(
            await valkey.get(ctok_key, timeout_seconds=timeout)
        )
        if limits.completion_tokens_per_min > 0 and completion_used >= limits.completion_tokens_per_min:
            _reject(
                "completion_tokens",
                _seconds_to_window_end(window_seconds),
                limits.completion_tokens_per_min,
            )

        # Fixed-window request count: atomic INCR + (re-)arm TTL.
        count = await valkey.incr_with_expire(
            req_key, ttl_seconds=window_seconds, timeout_seconds=timeout
        )
        if limits.requests_per_min > 0 and count > limits.requests_per_min:
            _reject("requests", _seconds_to_window_end(window_seconds), limits.requests_per_min)
    except ApiError:
        raise  # a genuine 429 — propagate, do NOT fail open
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN (allow)
        _failopen("enforce_pre", "valkey_unavailable", principal, error=str(exc))


async def debit_tokens(
    valkey: ValkeyClient | None,
    principal: Principal,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    settings: Settings | None = None,
) -> None:
    """Post-hoc token debit. Adds consumed tokens to this window's counters. Never raises.

    Call AFTER a completion (streaming or not) with the actual token counts. Enforcement
    happens on the NEXT request's :func:`enforce_pre`. FAIL-OPEN: a Valkey error just
    drops the debit (logged + counted) — it must never affect the response the client
    already received.

    Args:
        valkey: ``request.app.state.valkey`` (or ``None`` -> skip debit).
        principal: the authenticated caller (``tenant_id`` keys the counters).
        prompt_tokens: prompt/input tokens consumed by the completion (>= 0).
        completion_tokens: completion/output tokens consumed (>= 0).
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.rate_limit_enabled:
        return
    if valkey is None:
        _failopen("debit_tokens", "no_valkey_client", principal)
        return
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return

    window_seconds = settings.rate_limit_window_seconds
    window = _window_id(window_seconds)
    _req_key, ptok_key, ctok_key = _keys(settings.rate_limit_key_prefix, principal.tenant_id, window)
    timeout = settings.rate_limit_valkey_timeout_seconds

    try:
        if prompt_tokens > 0:
            await valkey.incrby_with_expire(
                ptok_key, prompt_tokens, ttl_seconds=window_seconds, timeout_seconds=timeout
            )
        if completion_tokens > 0:
            await valkey.incrby_with_expire(
                ctok_key, completion_tokens, ttl_seconds=window_seconds, timeout_seconds=timeout
            )
    except Exception as exc:  # noqa: BLE001 — debit is best-effort; never affects the response
        _failopen("debit_tokens", "valkey_unavailable", principal, error=str(exc))


def _to_int(raw: str | None) -> int:
    """Parse a Valkey counter string; absent/garbage -> 0."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _failopen(op: str, reason: str, principal: Principal, *, error: str | None = None) -> None:
    metrics.rate_limit_failopen_total.labels(op).inc()
    logger.warning(
        "rate_limit_failopen",
        op=op,
        reason=reason,
        tenant_id=principal.tenant_id,
        error=error,
        rate_limit_failopen=True,
    )
