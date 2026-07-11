"""Per-tenant fixed-window rate limiting (fail-open).

A single Valkey fixed-window request counter per tenant + dimension. :func:`enforce` is
called at the start of a guarded request: it atomically ``INCR`` + ``EXPIRE`` the
current-window counter and, if it exceeds the limit, raises 429 ``RATE_LIMIT_EXCEEDED``
with a ``Retry-After`` header.

FAIL-OPEN: any Valkey problem -> ALLOW. ``settings.rate_limit_enabled = false`` makes
:func:`enforce` a no-op.
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
    from ..core.valkey import ValkeyClient

logger = structlog.get_logger(__name__)


def _window_id(window_seconds: int) -> int:
    return int(time.time()) // window_seconds


def _seconds_to_window_end(window_seconds: int) -> int:
    now = time.time()
    end = (int(now) // window_seconds + 1) * window_seconds
    return max(1, int(end - now))


def _req_key(prefix: str, tenant_id: str, dimension: str, window: int) -> str:
    return f"{prefix}{dimension}:{tenant_id}:{window}"


def _reject(retry_after: int, limit: int, dimension: str) -> None:
    metrics.rate_limit_rejected_total.labels(dimension).inc()
    raise ApiError(
        ErrorCode.RATE_LIMIT_EXCEEDED,
        f"Rate limit exceeded (limit {limit} per minute for '{dimension}').",
        status_code=429,
        details={"dimension": dimension, "limit": limit, "retry_after_seconds": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


async def enforce(
    valkey: ValkeyClient | None,
    principal: Principal,
    *,
    dimension: str = "requests",
    limit: int | None = None,
    settings: Settings | None = None,
) -> None:
    """Pre-request rate-limit gate. Raises 429 when over limit. FAIL-OPEN on Valkey error."""
    settings = settings or get_settings()
    if not settings.rate_limit_enabled:
        return
    effective_limit = limit if limit is not None else settings.rate_limit_requests_per_min
    if valkey is None:
        _failopen("enforce", "no_valkey_client", principal)
        return

    window_seconds = settings.rate_limit_window_seconds
    window = _window_id(window_seconds)
    key = _req_key(settings.rate_limit_key_prefix, principal.tenant_id, dimension, window)
    timeout = settings.rate_limit_valkey_timeout_seconds

    try:
        count = await valkey.incr_with_expire(
            key, ttl_seconds=window_seconds, timeout_seconds=timeout
        )
        if effective_limit > 0 and count > effective_limit:
            _reject(_seconds_to_window_end(window_seconds), effective_limit, dimension)
    except ApiError:
        raise  # a genuine 429 — propagate, do NOT fail open
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN (allow)
        _failopen("enforce", "valkey_unavailable", principal, error=str(exc))


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
