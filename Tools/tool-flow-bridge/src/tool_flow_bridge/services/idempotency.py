"""Idempotency-Key replay for the /w/<slug>/mcp tools/call path (Contract-9 style, fail-open).

Valkey-backed, keyed by the client ``Idempotency-Key`` header + tenant + slug. This is
LOAD-BEARING for the bridge: workflow executions have side effects, and xAgent retries
5xx/transport failures with the SAME Idempotency-Key — replay-dedup prevents a workflow
from firing twice.

FAIL-OPEN: any Valkey problem (no client, connect error, timeout) -> proceed WITHOUT the
guarantee (no replay). ``settings.idempotency_enabled = false`` or an empty key makes
every function inert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings

if TYPE_CHECKING:
    from ..core.auth import Principal
    from ..core.valkey import ValkeyClient

logger = structlog.get_logger(__name__)

REPLAY_HEADER = "Idempotency-Replayed"


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def _record_key(prefix: str, tenant_id: str, scope: str, key: str) -> str:
    """Tenant + slug scoped idempotency record key."""
    return f"{prefix}{tenant_id}:{scope}:{key}"


def _lock_key(prefix: str, tenant_id: str, scope: str, key: str) -> str:
    """The in-flight lock key (distinct from the stored-result record key)."""
    return f"{prefix}{tenant_id}:{scope}:{key}:lock"


async def acquire_inflight(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    scope: str,
    settings: Settings | None = None,
) -> bool:
    """Try to claim the in-flight lock for this Idempotency-Key. Returns True if claimed (proceed),
    False if another request with the same key is already executing (caller should reject-and-retry).

    FAIL-OPEN: disabled / no key / no Valkey / Valkey error -> True (proceed without the guarantee),
    matching the replay path — availability over the dedup guarantee.
    """
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return True
    lock_key = _lock_key(settings.idempotency_key_prefix, principal.tenant_id, scope, key)
    try:
        return await valkey.set_if_absent(
            lock_key,
            "1",
            ttl_seconds=settings.idempotency_lock_ttl_seconds,
            timeout_seconds=settings.idempotency_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — fail open: proceed without the lock
        _failopen("acquire_inflight", "valkey_unavailable", principal, error=str(exc))
        return True


async def release_inflight(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    scope: str,
    settings: Settings | None = None,
) -> None:
    """Release the in-flight lock (best-effort) so a legitimate later retry can proceed. The TTL is
    the backstop if this never runs (e.g. the process dies mid-flight)."""
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return
    lock_key = _lock_key(settings.idempotency_key_prefix, principal.tenant_id, scope, key)
    try:
        await valkey.delete(lock_key, timeout_seconds=settings.idempotency_valkey_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — best-effort release; TTL is the backstop
        _failopen("release_inflight", "valkey_unavailable", principal, error=str(exc))


async def get_replay(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    scope: str,
    settings: Settings | None = None,
) -> StoredResponse | None:
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return None

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, scope, key)
    timeout = settings.idempotency_valkey_timeout_seconds
    try:
        raw = await valkey.get(record_key, timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001 — fail open: no replay
        _failopen("get_replay", "valkey_unavailable", principal, error=str(exc))
        return None
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    body = record.get("body")
    if not isinstance(body, dict):
        return None
    metrics.idempotency_replayed_total.inc()
    return StoredResponse(status_code=int(record.get("status_code", 200)), body=body)


async def store(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    status_code: int,
    body: dict[str, Any],
    *,
    scope: str,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, scope, key)
    record = json.dumps({"status_code": status_code, "body": body})
    try:
        await valkey.set(
            record_key,
            record,
            ttl_seconds=settings.idempotency_ttl_seconds,
            timeout_seconds=settings.idempotency_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort store
        _failopen("store", "valkey_unavailable", principal, error=str(exc))


def _failopen(op: str, reason: str, principal: Principal, *, error: str | None = None) -> None:
    metrics.idempotency_failopen_total.labels(op).inc()
    logger.warning(
        "idempotency_failopen",
        op=op,
        reason=reason,
        tenant_id=principal.tenant_id,
        error=error,
        idempotency_failopen=True,
    )
