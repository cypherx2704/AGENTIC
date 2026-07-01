"""Idempotency (Contract 9) — MANDATORY on ``/ingest/finalize``.

Finalize triggers embedding charges (worker -> llms). A client retry must not double-enqueue
or double-bill. Keyed by ``{prefix}{tenant}:{kb}:finalize:{idempotency_key}`` in Valkey:

  * Miss        -> claim the ``in_flight`` slot (SET NX EX), proceed; store ``completed`` +
                   the response body after the outbox txn commits.
  * ``in_flight``-> 409 IDEMPOTENCY_REQUEST_IN_FLIGHT + Retry-After: 2.
  * ``completed``-> replay the cached body with ``Idempotent-Replay: true``; NO new enqueue.
  * Valkey down -> FAIL OPEN (proceed) + telemetry. The worker-side ``(doc_id, content_sha)``
                   dedup is the secondary defence for the outage window.
"""

from __future__ import annotations

import enum
import json

import structlog

from ..core import metrics
from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db.valkey import ValkeyClient

logger = structlog.get_logger(__name__)

REPLAY_HEADER = "Idempotent-Replay"


class BeginState(enum.Enum):
    NEW = "new"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILOPEN = "failopen"


def _key(settings: Settings, principal: Principal, kb_id: str, idem_key: str) -> str:
    return f"{settings.idempotency_key_prefix}{principal.tenant_id}:{kb_id}:finalize:{idem_key}"


async def begin(
    valkey: ValkeyClient | None,
    idem_key: str,
    principal: Principal,
    kb_id: str,
    *,
    settings: Settings,
) -> BeginState:
    if not settings.idempotency_enabled or valkey is None:
        metrics.idempotency_skipped_total.labels("begin").inc()
        return BeginState.FAILOPEN
    key = _key(settings, principal, kb_id, idem_key)
    try:
        claimed = await valkey.set_if_absent(
            key,
            json.dumps({"status": "in_flight"}),
            ttl_seconds=settings.idempotency_in_flight_ttl_seconds,
            timeout_seconds=settings.idempotency_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down: fail open
        logger.warning("idempotency_begin_failopen", error=str(exc))
        metrics.idempotency_skipped_total.labels("begin").inc()
        return BeginState.FAILOPEN
    if claimed:
        return BeginState.NEW
    # Already present — read status.
    try:
        raw = await valkey.get(key, timeout_seconds=settings.idempotency_valkey_timeout_seconds)
    except Exception:  # noqa: BLE001 — treat as in-flight to be safe
        return BeginState.IN_FLIGHT
    if not raw:
        return BeginState.IN_FLIGHT
    try:
        status = json.loads(raw).get("status")
    except (ValueError, TypeError):
        return BeginState.IN_FLIGHT
    return BeginState.COMPLETED if status == "completed" else BeginState.IN_FLIGHT


async def get_replay(
    valkey: ValkeyClient | None,
    idem_key: str,
    principal: Principal,
    kb_id: str,
    *,
    settings: Settings,
) -> dict | None:
    if valkey is None:
        return None
    key = _key(settings, principal, kb_id, idem_key)
    try:
        raw = await valkey.get(key, timeout_seconds=settings.idempotency_valkey_timeout_seconds)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if record.get("status") != "completed":
        return None
    metrics.idempotency_replayed_total.inc()
    return record.get("body")


async def complete(
    valkey: ValkeyClient | None,
    idem_key: str,
    principal: Principal,
    kb_id: str,
    body: dict,
    *,
    settings: Settings,
) -> None:
    if not settings.idempotency_enabled or valkey is None:
        return
    key = _key(settings, principal, kb_id, idem_key)
    try:
        await valkey.set(
            key,
            json.dumps({"status": "completed", "body": body}),
            ttl_seconds=settings.idempotency_ttl_seconds,
            timeout_seconds=settings.idempotency_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — fail open (response already produced)
        logger.warning("idempotency_complete_failopen", error=str(exc))
        metrics.idempotency_skipped_total.labels("complete").inc()


async def release(
    valkey: ValkeyClient | None,
    idem_key: str,
    principal: Principal,
    kb_id: str,
    *,
    settings: Settings,
) -> None:
    """Release a claimed ``in_flight`` slot WITHOUT marking it completed.

    Called on a RETRYABLE failure path (object-not-found, doc-not-found, tenant-prefix
    mismatch, enqueue error) so a client retry is not blocked by a stale ``in_flight`` key
    for the full TTL (which would surface as a spurious 409 IDEMPOTENCY_REQUEST_IN_FLIGHT for
    ``idempotency_in_flight_ttl_seconds``). Only deletes a slot still in the ``in_flight``
    state — never a ``completed`` record (so a concurrent winner's result is preserved).
    Fail-open + telemetry on Valkey errors (consistent with ``begin``/``complete``).
    """
    if not settings.idempotency_enabled or valkey is None:
        return
    key = _key(settings, principal, kb_id, idem_key)
    try:
        raw = await valkey.get(key, timeout_seconds=settings.idempotency_valkey_timeout_seconds)
        if raw:
            try:
                status = json.loads(raw).get("status")
            except (ValueError, TypeError):
                status = "in_flight"
            if status == "completed":
                return  # never clobber a completed record
        await valkey.delete(key, timeout_seconds=settings.idempotency_valkey_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — Valkey down: fail open (worker dedup is the backstop)
        logger.warning("idempotency_release_failopen", error=str(exc))
        metrics.idempotency_skipped_total.labels("release").inc()


def raise_in_flight() -> None:
    raise ApiError(
        ErrorCode.IDEMPOTENCY_REQUEST_IN_FLIGHT,
        "A finalize request with this Idempotency-Key is already in flight.",
        headers={"Retry-After": "2"},
    )
