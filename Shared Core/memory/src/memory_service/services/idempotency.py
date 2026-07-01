"""Idempotency-Key short-circuit for POST /v1/memories (Contract-9).

THE store-path requirement: an ``Idempotency-Key`` replay must NOT re-embed. So the key
is checked FIRST — before the embeddings call and before any DB work:

* ``begin``      claims an ``in_flight`` slot (SET NX). Returns:
    * NEW        — proceed (the caller embeds + stores, then ``complete``).
    * IN_FLIGHT  — a twin request is mid-flight -> 409.
    * COMPLETED  — a finished response is cached -> ``get_replay`` returns it verbatim.
    * FAILOPEN   — Valkey unavailable -> proceed WITHOUT the guarantee (availability wins).
* ``get_replay`` returns the stored ``(status_code, body)`` for a COMPLETED key.
* ``complete``   stores the finished response under the key for future replay.

Keys are namespaced by the OWNING principal so two principals can't collide on the same
client-chosen key. Everything FAILS OPEN: with no Valkey / on any Valkey error the store
proceeds (it just loses the dedup-on-retry guarantee). The whole module is a no-op when
``idempotency`` is disabled.
"""

from __future__ import annotations

import enum
import json
from typing import TYPE_CHECKING, Any

import structlog

from ..core.errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..core.auth import Principal

logger = structlog.get_logger(__name__)

REPLAY_HEADER = "Idempotency-Replayed"
_KEY_PREFIX = "cypherx:mem:idem:"
_IN_FLIGHT_TTL = 300
_COMPLETED_TTL = 86400


class BeginState(enum.Enum):
    NEW = "new"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILOPEN = "failopen"


class Replay:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body


def _key(principal: Principal, idem_key: str) -> str:
    ptype, pid = principal.memory_principal
    return f"{_KEY_PREFIX}{principal.tenant_id}:{ptype}:{pid}:{idem_key}"


async def begin(valkey: object, idem_key: str, principal: Principal) -> BeginState:
    """Claim the in-flight slot. NEW/IN_FLIGHT/COMPLETED/FAILOPEN. Never raises."""
    if valkey is None:
        return BeginState.FAILOPEN
    key = _key(principal, idem_key)
    try:
        claimed = await valkey.set_if_absent(  # type: ignore[attr-defined]
            key, json.dumps({"state": "in_flight"}), ttl_seconds=_IN_FLIGHT_TTL
        )
        if claimed:
            return BeginState.NEW
        raw = await valkey.get(key)  # type: ignore[attr-defined]
        if raw:
            try:
                state = json.loads(raw).get("state")
            except (ValueError, TypeError):
                state = None
            if state == "completed":
                return BeginState.COMPLETED
        return BeginState.IN_FLIGHT
    except Exception as exc:  # noqa: BLE001 — Valkey down: FAIL OPEN
        logger.warning("idempotency_begin_failopen", error=str(exc))
        return BeginState.FAILOPEN


async def get_replay(valkey: object, idem_key: str, principal: Principal) -> Replay | None:
    if valkey is None:
        return None
    try:
        raw = await valkey.get(_key(principal, idem_key))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning("idempotency_replay_failopen", error=str(exc))
        return None
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if rec.get("state") != "completed" or "body" not in rec:
        return None
    return Replay(int(rec.get("status_code", 200)), rec["body"])


async def complete(
    valkey: object,
    idem_key: str,
    principal: Principal,
    status_code: int,
    body: dict[str, Any],
) -> None:
    if valkey is None:
        return
    try:
        await valkey.set(  # type: ignore[attr-defined]
            _key(principal, idem_key),
            json.dumps({"state": "completed", "status_code": status_code, "body": body}),
            ttl_seconds=_COMPLETED_TTL,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("idempotency_complete_failopen", error=str(exc))


def raise_in_flight() -> None:
    raise ApiError(
        ErrorCode.IDEMPOTENCY_REQUEST_IN_FLIGHT,
        "A request with this Idempotency-Key is already in flight.",
    )
