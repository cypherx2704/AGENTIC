"""Idempotency for POST /v1/chat/completions (WP05, Contract-9).

Valkey-backed, keyed by the client ``Idempotency-Key`` header + tenant. One stored
record per key moves through two states:

    in_flight  -> completed

State machine (the chat path drives it via the functions below):

* **First request** with a key: :func:`begin` claims the slot with ``SET key NX`` ->
  :data:`BeginState.NEW`. Proceed with the completion, then call :func:`complete`.
* **Concurrent duplicate** while ``in_flight``: :func:`begin` -> :data:`BeginState.IN_FLIGHT`
  -> the chat path raises 409 ``IDEMPOTENCY_REQUEST_IN_FLIGHT`` (helper
  :func:`raise_in_flight`).
* **Duplicate after ``completed``**: :func:`begin` -> :data:`BeginState.COMPLETED`; the
  chat path calls :func:`get_replay` and re-serves the stored response with header
  ``Idempotency-Replayed: true`` (constant :data:`REPLAY_HEADER`).

**Streams (``stream=true``) are recorded-but-replay-EXEMPT.** An SSE body cannot be
replayed coherently from a single cached blob, so the decided rule is: do NOT store
and do NOT replay streamed responses. The chat agent should simply SKIP idempotency
for streaming requests (don't call :func:`begin`/:func:`complete`), OR call
:func:`begin` but never :func:`complete` (the ``in_flight`` marker self-expires via
``idempotency_in_flight_ttl_seconds``). :func:`get_replay` always returns ``None`` for a
record whose stored ``stream`` flag is true, so even a marked stream never replays.
The simplest integration: ``if body.stream: skip idempotency entirely``.

FAIL-OPEN: any Valkey problem (no client, connect error, timeout) -> proceed WITHOUT
the idempotency guarantee, log ``idempotency_failopen=true``, bump
``idempotency_failopen_total``. :func:`begin` returns :data:`BeginState.FAILOPEN` so the
caller proceeds as if NEW (no conflict, no replay). ``settings.idempotency_enabled =
false`` makes all functions inert (begin -> FAILOPEN, get_replay -> None, complete -> noop).

Public API (call these from the chat path):

    state = await begin(valkey, key, principal, stream=body.stream, settings=...)
        -> BeginState         # NEW | IN_FLIGHT | COMPLETED | FAILOPEN
    replay = await get_replay(valkey, key, principal, settings=...)
        -> StoredResponse | None
    await complete(valkey, key, principal, status_code, body_json, settings=...)

``valkey`` is ``request.app.state.valkey`` (may be ``None``). ``key`` is the raw
``Idempotency-Key`` request header (caller passes ``None`` / empty -> all functions
treat it as "no idempotency": begin -> FAILOPEN, get_replay -> None, complete -> noop).
``body_json`` is the JSON-serializable response body dict (e.g.
``response.model_dump(by_alias=True)``).
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..core.auth import Principal
    from ..db.valkey import ValkeyClient

logger = structlog.get_logger(__name__)

# Header set on a replayed response so clients can tell a cache hit from a fresh call.
REPLAY_HEADER = "Idempotency-Replayed"

_STATE_IN_FLIGHT = "in_flight"
_STATE_COMPLETED = "completed"


class BeginState(enum.Enum):
    """Outcome of :func:`begin`."""

    NEW = "new"  # slot claimed; proceed with the completion, then call complete()
    IN_FLIGHT = "in_flight"  # a duplicate is already running -> caller raises 409
    COMPLETED = "completed"  # a prior identical request finished -> caller replays
    FAILOPEN = "failopen"  # idempotency unavailable/disabled -> proceed as NEW, no guarantee


@dataclass(frozen=True)
class StoredResponse:
    """A replayable completed response (non-stream)."""

    status_code: int
    body: dict[str, Any]


def _record_key(prefix: str, tenant_id: str, key: str) -> str:
    """Tenant-scoped idempotency record key (tenant prevents cross-tenant key reuse)."""
    return f"{prefix}{tenant_id}:{key}"


async def begin(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    stream: bool = False,
    settings: Settings | None = None,
) -> BeginState:
    """Claim the idempotency slot for ``key``; return the :class:`BeginState`.

    * No key / disabled / no Valkey -> :data:`BeginState.FAILOPEN` (proceed as NEW).
    * Slot free -> ``SET NX`` an ``in_flight`` marker (TTL
      ``idempotency_in_flight_ttl_seconds``) -> :data:`BeginState.NEW`.
    * Slot held & ``completed`` -> :data:`BeginState.COMPLETED` (caller replays via
      :func:`get_replay`).
    * Slot held & ``in_flight`` -> :data:`BeginState.IN_FLIGHT` (caller raises 409 via
      :func:`raise_in_flight`).

    FAIL-OPEN on any Valkey error -> :data:`BeginState.FAILOPEN`.

    Args:
        valkey: ``request.app.state.valkey`` (or ``None``).
        key: raw ``Idempotency-Key`` header (``None``/empty -> FAILOPEN).
        principal: the authenticated caller (``tenant_id`` scopes the record).
        stream: whether the request is streaming (recorded in the marker; streams are
            replay-exempt — see module docstring).
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None:
        if key and valkey is None and settings.idempotency_enabled:
            _failopen("begin", "no_valkey_client", principal)
        return BeginState.FAILOPEN

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, key)
    marker = json.dumps({"state": _STATE_IN_FLIGHT, "stream": bool(stream)})
    timeout = settings.idempotency_valkey_timeout_seconds

    try:
        claimed = await valkey.set_if_absent(
            record_key,
            marker,
            ttl_seconds=settings.idempotency_in_flight_ttl_seconds,
            timeout_seconds=timeout,
        )
        if claimed:
            return BeginState.NEW
        # Slot already exists — inspect its state.
        existing = await valkey.get(record_key, timeout_seconds=timeout)
        state = _parse_state(existing)
        if state == _STATE_COMPLETED:
            return BeginState.COMPLETED
        return BeginState.IN_FLIGHT
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN (proceed)
        _failopen("begin", "valkey_unavailable", principal, error=str(exc))
        return BeginState.FAILOPEN


async def get_replay(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    *,
    settings: Settings | None = None,
) -> StoredResponse | None:
    """Return the stored completed response for ``key``, or ``None`` if not replayable.

    Returns ``None`` when: idempotency is disabled, no key, no Valkey, the record is
    absent/``in_flight``, the stored record is a STREAM (replay-exempt), or on any
    Valkey error (fail-open). On a successful replay it bumps
    ``idempotency_replayed_total``. The caller adds the :data:`REPLAY_HEADER` to the
    re-served response.
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
    if not isinstance(record, dict) or record.get("state") != _STATE_COMPLETED:
        return None
    if record.get("stream"):  # streams are recorded-but-replay-exempt
        return None
    body = record.get("body")
    if not isinstance(body, dict):
        return None
    metrics.idempotency_replayed_total.inc()
    return StoredResponse(status_code=int(record.get("status_code", 200)), body=body)


async def complete(
    valkey: ValkeyClient | None,
    key: str | None,
    principal: Principal,
    status_code: int,
    body: dict[str, Any],
    *,
    stream: bool = False,
    settings: Settings | None = None,
) -> None:
    """Store the finished response under ``key`` for future replay (state ``completed``).

    Overwrites the ``in_flight`` marker with the completed record (TTL
    ``idempotency_ttl_seconds``, default 24h). NEVER raises — a storage failure just
    means future duplicates won't replay (logged + counted), which is safe.

    **Streams**: if ``stream=True`` this is a NO-OP (streams are not stored/replayed;
    the ``in_flight`` marker self-expires). The chat agent may simply skip calling
    :func:`complete` for streaming requests.

    Args:
        valkey: ``request.app.state.valkey`` (or ``None``).
        key: raw ``Idempotency-Key`` header (``None``/empty -> noop).
        principal: the authenticated caller.
        status_code: HTTP status of the completed response (e.g. 200).
        body: JSON-serializable response body (e.g. ``response.model_dump(by_alias=True)``).
        stream: whether the request was streaming -> noop when true.
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.idempotency_enabled or not key or valkey is None or stream:
        return

    record_key = _record_key(settings.idempotency_key_prefix, principal.tenant_id, key)
    record = json.dumps(
        {"state": _STATE_COMPLETED, "stream": False, "status_code": status_code, "body": body}
    )
    try:
        await valkey.set(
            record_key,
            record,
            ttl_seconds=settings.idempotency_ttl_seconds,
            timeout_seconds=settings.idempotency_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort store; future dups just won't replay
        _failopen("complete", "valkey_unavailable", principal, error=str(exc))


def raise_in_flight() -> None:
    """Raise the Contract-2 409 for a duplicate that is still ``in_flight``.

    Call when :func:`begin` returns :data:`BeginState.IN_FLIGHT`.
    """
    metrics.idempotency_conflict_total.inc()
    raise ApiError(
        ErrorCode.IDEMPOTENCY_REQUEST_IN_FLIGHT,
        "A request with this Idempotency-Key is already in progress.",
        status_code=409,
    )


def _parse_state(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return record.get("state") if isinstance(record, dict) else None


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
