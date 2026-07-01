"""Idempotency-Key replay for POST /mcp/v1/invoke (Contract-9 style, fail-open).

Valkey-backed, keyed by the client ``Idempotency-Key`` header + tenant. The invoke
handler drives it:

* Before running the search, :func:`get_replay` returns a stored completed response for
  the same (tenant, key) — replay it verbatim with header ``Idempotency-Replayed: true``.
* After a successful search, :func:`store` writes the response under the key (TTL
  ``idempotency_ttl_seconds``, default 24h) for future replay.

FAIL-OPEN: any Valkey problem (no client, connect error, timeout) -> proceed WITHOUT the
guarantee (no replay / a store that silently drops), log ``idempotency_failopen=true``,
bump ``tws_idempotency_failopen_total``. ``settings.idempotency_enabled = false`` or an
empty key makes every function inert.
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

# Header set on a replayed response so clients can tell a cache hit from a fresh call.
REPLAY_HEADER = "Idempotency-Replayed"


@dataclass(frozen=True)
class StoredResponse:
    """A replayable completed response."""

    status_code: int
    body: dict[str, Any]


def _record_key(prefix: str, tenant_id: str, key: str) -> str:
    """Tenant-scoped idempotency record key (tenant prevents cross-tenant key reuse)."""
    return f"{prefix}{tenant_id}:{key}"


async def get_replay(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    settings: Settings | None = None,
) -> StoredResponse | None:
    """Return the stored completed response for ``key``, or ``None`` if not replayable.

    Returns ``None`` when idempotency is disabled, no key, no Valkey, the record is
    absent, or on any Valkey error (fail-open). On a successful replay it bumps
    ``tws_idempotency_replayed_total``. The caller adds :data:`REPLAY_HEADER`.
    """
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return None

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, key)
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
    settings: Settings | None = None,
) -> None:
    """Store the finished response under ``key`` for future replay. NEVER raises —
    a storage failure just means future duplicates won't replay (logged + counted)."""
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        return

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, key)
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
