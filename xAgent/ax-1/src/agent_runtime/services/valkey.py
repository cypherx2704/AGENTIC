"""Lazy async Valkey client (SOFT dependency — wiring foundation, WP02; signals, WP08).

Valkey carries the agent-config cache, cancellation signals, the authorize-verdict
cache and Idempotency-Key replay. WP02 landed the shared wiring; WP08 adds the
task-lifecycle helpers (cooperative cancel, Contract-9 idempotency, authorize cache).
The wiring is the same shape the llms-gateway uses:

  * ``VALKEY_URL`` setting (``core.config.Settings.valkey_url``);
  * a LAZY async client (the connection is created on first use, never at import or
    construction time, so a Valkey outage can never fail boot);
  * a ``/readyz`` SOFT-report (``ping()`` outcome surfaces in the checks map but NEVER
    gates readiness — Valkey is a soft dependency per the Phase 9 K8s spec);
  * the ``xagent_valkey_up`` gauge (1/0 from the last ping).

CONFIGURED-vs-UNIT distinction (WP08): the lifespan wires a real ``ValkeyClient``. Under
test the conftest swaps ``app.state.valkey`` for a network-free double that does NOT
implement the WP08 helper methods. Callers therefore treat "no real ``ValkeyClient`` on
``app.state``" as the feature being DISABLED (idempotency allow-through, no cancel store,
authorize fail-open) and only a CONFIGURED-but-erroring client triggers the fail-closed
503 path. The helpers here RAISE on any Valkey error so the caller can make that choice;
they never swallow errors into a false "allow".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis

from ..core import metrics

logger = structlog.get_logger(__name__)

# Connection/IO timeouts (seconds) for the soft-dependency client: a down Valkey must
# fail a ping fast, never hang a readiness probe.
_SOCKET_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class IdempotencyRecord:
    """A stored Contract-9 idempotency record for one (tenant, Idempotency-Key).

    ``state`` is ``in_flight`` while the original request executes, then ``completed``
    once its terminal response is stored. ``status_code`` + ``response`` carry the stored
    HTTP reply replayed on a duplicate hit (only meaningful when ``state == 'completed'``).
    """

    state: str  # in_flight | completed
    status_code: int | None = None
    response: dict[str, Any] | None = None
    # Contract-9 conflict detection: a stable hash of the ORIGINAL request body. A duplicate
    # hit carrying a DIFFERENT fingerprint is a key reuse with a different payload -> 409
    # IDEMPOTENCY_KEY_CONFLICT (the caller compares; None = legacy record, never conflicts).
    fingerprint: str | None = None


@dataclass(frozen=True)
class RevocationState:
    """The three shared kill-switch values for one token (Component 3c, WP03).

    Mirrors the shared revocation scheme every verifier reads:
      * ``jti_revoked`` — ``<prefix>jti:{jti}`` exists (this specific token revoked);
      * ``kid_revoked`` — ``<prefix>kid:{kid}`` exists (the signing key is poisoned);
      * ``agent_epoch`` — int value of ``<prefix>agent:{agent_id}`` (revoke-all cutoff;
        reject when ``token.iat < agent_epoch``), or ``None`` when the key is absent.
    """

    jti_revoked: bool
    kid_revoked: bool
    agent_epoch: int | None


class ValkeyClient:
    """Lazily-constructed async Valkey (Redis-protocol) client + soft health probe."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Redis | None = None

    def client(self) -> Redis:
        """Return the shared async client, creating it on first use (lazy)."""
        if self._client is None:
            self._client = Redis.from_url(
                self._url,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
        return self._client

    async def ping(self) -> bool:
        """Soft health probe: True on PONG; False (never raises) otherwise.

        Updates the ``xagent_valkey_up`` gauge on every call. Used by ``/readyz`` as a
        REPORT-ONLY check — readiness gates on Postgres + Auth JWKS only.
        """
        try:
            await self.client().ping()
        except Exception as exc:  # noqa: BLE001 — soft dependency; a probe must never raise
            metrics.valkey_up.set(0)
            logger.warning("valkey_ping_failed", error=str(exc))
            return False
        metrics.valkey_up.set(1)
        return True

    async def revocation_lookup(
        self,
        *,
        prefix: str,
        jti: str | None,
        kid: str | None,
        agent_id: str | None,
        timeout_seconds: float,
    ) -> RevocationState:
        """Read the three shared revocation keys for one token in a single round-trip.

        Returns a :class:`RevocationState`. RAISES on any Valkey error / timeout so the
        caller can FAIL OPEN (revocation is a defense-in-depth kill-switch — availability
        wins). A short ``timeout_seconds`` budget guarantees a slow Valkey never stalls
        the request. Absent keys are NOT an error: a missing key simply means "not
        revoked" (``False`` / ``None``).
        """
        # Build a stable key list (Nones omitted) so we can MGET them in one shot and map
        # the results back positionally — one network round-trip for all three keys.
        keys: list[str] = []
        idx_jti = idx_kid = idx_agent = -1
        if jti:
            idx_jti = len(keys)
            keys.append(f"{prefix}jti:{jti}")
        if kid:
            idx_kid = len(keys)
            keys.append(f"{prefix}kid:{kid}")
        if agent_id:
            idx_agent = len(keys)
            keys.append(f"{prefix}agent:{agent_id}")

        if not keys:
            # Nothing identifiable to look up (e.g. a token with no jti/kid/agent_id):
            # treat as not-revoked without touching Valkey.
            return RevocationState(jti_revoked=False, kid_revoked=False, agent_epoch=None)

        async with asyncio.timeout(timeout_seconds):
            values = await self.client().mget(keys)

        def _present(i: int) -> bool:
            return i >= 0 and values[i] is not None

        agent_epoch: int | None = None
        if idx_agent >= 0 and values[idx_agent] is not None:
            agent_epoch = _coerce_epoch(values[idx_agent])

        return RevocationState(
            jti_revoked=_present(idx_jti),
            kid_revoked=_present(idx_kid),
            agent_epoch=agent_epoch,
        )

    # ── WP08: cooperative cancel signal ───────────────────────────────────────────
    async def set_cancel_signal(
        self, *, prefix: str, tenant_id: str, task_id: str, ttl_seconds: int, timeout_seconds: float
    ) -> None:
        """Set the per-task cancel flag (``<prefix>cancel:{tenant}:{task}`` = '1', TTL).

        RAISES on any Valkey error / timeout so the cancel endpoint can return 503 — we
        must NOT report "cancel accepted" if the signal never landed (decided semantics).
        """
        key = self._cancel_key(prefix, tenant_id, task_id)
        async with asyncio.timeout(timeout_seconds):
            await self.client().set(key, "1", ex=ttl_seconds)

    async def is_cancelled(
        self, *, prefix: str, tenant_id: str, task_id: str, timeout_seconds: float
    ) -> bool:
        """Return True iff the cancel flag is present. RAISES on Valkey error/timeout.

        The pipeline poller treats a RAISE as "cannot confirm cancel" and proceeds
        (availability of the run wins; the sweeper/timeout remain the backstop).
        """
        key = self._cancel_key(prefix, tenant_id, task_id)
        async with asyncio.timeout(timeout_seconds):
            value = await self.client().get(key)
        return value is not None

    async def clear_cancel_signal(
        self, *, prefix: str, tenant_id: str, task_id: str, timeout_seconds: float
    ) -> None:
        """Best-effort delete of the cancel flag (called once a task reaches terminal).

        Never raises — a stale flag self-evicts via its TTL, so a failed delete is benign.
        """
        key = self._cancel_key(prefix, tenant_id, task_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.client().delete(key)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort (TTL backstops it)
            logger.warning("cancel_signal_clear_failed", task_id=task_id, error=str(exc))

    @staticmethod
    def _cancel_key(prefix: str, tenant_id: str, task_id: str) -> str:
        return f"{prefix}cancel:{tenant_id}:{task_id}"

    # ── WP08: Contract-9 idempotency store ─────────────────────────────────────────
    async def idempotency_reserve(
        self,
        *,
        prefix: str,
        tenant_id: str,
        key: str,
        ttl_seconds: int,
        timeout_seconds: float,
        fingerprint: str | None = None,
    ) -> IdempotencyRecord | None:
        """Atomically reserve an idempotency key, or return the EXISTING record.

        Uses ``SET NX`` to claim the key with an ``in_flight`` marker (carrying the request
        ``fingerprint`` so a later duplicate with a different body can be detected). Returns
        ``None`` when the reservation was freshly taken (caller proceeds + later stores the
        response). Returns the stored :class:`IdempotencyRecord` when the key already
        existed — the caller then replays (``completed``) or rejects (``in_flight`` -> 409).

        RAISES on any Valkey error / timeout (FAIL-CLOSED 503 at the caller).
        """
        rkey = self._idem_key(prefix, tenant_id, key)
        marker = json.dumps({"state": "in_flight", "fingerprint": fingerprint})
        async with asyncio.timeout(timeout_seconds):
            won = await self.client().set(rkey, marker, nx=True, ex=ttl_seconds)
            if won:
                return None
            raw = await self.client().get(rkey)
        return self._parse_idem(raw)

    async def idempotency_complete(
        self,
        *,
        prefix: str,
        tenant_id: str,
        key: str,
        status_code: int,
        response: dict[str, Any],
        ttl_seconds: int,
        timeout_seconds: float,
        fingerprint: str | None = None,
    ) -> None:
        """Overwrite the reservation with the terminal response for future replays.

        Best-effort on the response leg: a failure here only costs a replay (the original
        already executed + responded), so it logs + swallows rather than 503-ing a
        successful request. The reservation's TTL bounds the orphaned ``in_flight`` window.
        The ``fingerprint`` is preserved so a post-completion duplicate with a different body
        still 409s rather than replaying the wrong response.
        """
        rkey = self._idem_key(prefix, tenant_id, key)
        record = json.dumps(
            {
                "state": "completed",
                "status_code": int(status_code),
                "response": response,
                "fingerprint": fingerprint,
            }
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.client().set(rkey, record, ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001 — original already responded; replay is the only cost
            logger.warning("idempotency_complete_failed", error=str(exc))

    async def idempotency_release(
        self, *, prefix: str, tenant_id: str, key: str, timeout_seconds: float
    ) -> None:
        """Delete a stuck ``in_flight`` reservation when the original run failed to store.

        Best-effort: lets a retry proceed immediately instead of waiting out the TTL.
        Never raises.
        """
        rkey = self._idem_key(prefix, tenant_id, key)
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.client().delete(rkey)
        except Exception as exc:  # noqa: BLE001 — TTL backstops a failed release
            logger.warning("idempotency_release_failed", error=str(exc))

    @staticmethod
    def _idem_key(prefix: str, tenant_id: str, key: str) -> str:
        return f"{prefix}idem:{tenant_id}:{key}"

    @staticmethod
    def _parse_idem(raw: object) -> IdempotencyRecord:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if not raw:
            # Key vanished between the failed NX and the GET (TTL race): treat as in_flight
            # so the duplicate is rejected rather than wrongly proceeding without a guard.
            return IdempotencyRecord(state="in_flight")
        try:
            data = json.loads(str(raw))
        except (ValueError, TypeError):
            return IdempotencyRecord(state="in_flight")
        return IdempotencyRecord(
            state=str(data.get("state", "in_flight")),
            status_code=data.get("status_code"),
            response=data.get("response"),
            fingerprint=data.get("fingerprint"),
        )

    # ── WP08: authorize-verdict cache (60s TTL) ────────────────────────────────────
    async def get_authorize_verdict(
        self, *, prefix: str, tenant_id: str, agent_id: str, action: str, timeout_seconds: float
    ) -> bool | None:
        """Return the cached allow/deny verdict, or ``None`` on a cache miss.

        RAISES on Valkey error/timeout so the caller can decide its fail posture
        (fail-open for availability, per the documented WP08 stance).
        """
        key = self._authz_key(prefix, tenant_id, agent_id, action)
        async with asyncio.timeout(timeout_seconds):
            value = await self.client().get(key)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        return str(value) == "1"

    async def set_authorize_verdict(
        self,
        *,
        prefix: str,
        tenant_id: str,
        agent_id: str,
        action: str,
        allowed: bool,
        ttl_seconds: int,
        timeout_seconds: float,
    ) -> None:
        """Cache an allow/deny verdict for ``ttl_seconds`` (60s -> revocation within 60s).

        Best-effort: a failed write only costs an extra Auth call next time. Never raises.
        """
        key = self._authz_key(prefix, tenant_id, agent_id, action)
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.client().set(key, "1" if allowed else "0", ex=ttl_seconds)
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            logger.warning("authorize_verdict_cache_failed", error=str(exc))

    @staticmethod
    def _authz_key(prefix: str, tenant_id: str, agent_id: str, action: str) -> str:
        return f"{prefix}authz:{tenant_id}:{agent_id}:{action}"

    # ── WP12: per-task SSE event pub/sub ───────────────────────────────────────────
    # A per-task Valkey Pub/Sub channel carries the live step/stage progress + the
    # terminal result for the SSE relay endpoint (GET /v1/tasks/{id}/stream). The
    # producer (the pipeline / api layer) PUBLISHES JSON event frames; the SSE endpoint
    # SUBSCRIBES and relays each frame as a Server-Sent Event. Pub/Sub is SOFT: a publish
    # failure is swallowed (the polling fallback still surfaces progress), and a subscribe
    # that cannot reach Valkey yields nothing so the endpoint degrades to polling.
    async def publish_task_event(
        self,
        *,
        prefix: str,
        tenant_id: str,
        task_id: str,
        event: dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        """Publish one SSE event frame to the per-task channel (best-effort; never raises).

        The pipeline (or the api layer) calls this to push a step/stage/terminal event to
        any live SSE subscriber. A failure to publish is logged + swallowed — Pub/Sub is a
        latency optimisation over the polling fallback, never a correctness dependency, so
        a Valkey hiccup must not disturb the task run.
        """
        channel = self._event_channel(prefix, tenant_id, task_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.client().publish(channel, json.dumps(event))
        except Exception as exc:  # noqa: BLE001 — Pub/Sub is soft; the poll fallback covers it
            logger.warning("task_event_publish_failed", task_id=task_id, error=str(exc))

    async def subscribe_task_events(
        self, *, prefix: str, tenant_id: str, task_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-generate decoded SSE event frames published to the per-task channel.

        Subscribes to ``<prefix>events:{tenant}:{task}`` and yields each JSON-decoded
        ``message`` payload as it arrives. RAISES on a connect/subscribe failure so the
        SSE endpoint can fall back to polling (it treats a raise as "Pub/Sub unavailable"
        and switches to row/step snapshots). The caller is responsible for breaking out of
        the iteration (e.g. on a terminal frame or client disconnect); the ``finally`` here
        unsubscribes + closes the pubsub so no connection leaks.
        """
        channel = self._event_channel(prefix, tenant_id, task_id)
        pubsub = self.client().pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message is None or message.get("type") != "message":
                    continue  # skip subscribe/unsubscribe confirmations
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "ignore")
                if not data:
                    continue
                try:
                    yield json.loads(str(data))
                except (ValueError, TypeError):
                    logger.warning("task_event_decode_failed", task_id=task_id)
                    continue
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception as exc:  # noqa: BLE001 — teardown only
                logger.warning("task_event_unsubscribe_failed", task_id=task_id, error=str(exc))

    @staticmethod
    def _event_channel(prefix: str, tenant_id: str, task_id: str) -> str:
        return f"{prefix}events:{tenant_id}:{task_id}"

    async def aclose(self) -> None:
        """Close the underlying client if it was ever created."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001 — shutdown must never raise
                logger.warning("valkey_close_failed", error=str(exc))
            self._client = None


def _coerce_epoch(value: object) -> int | None:
    """Parse a ``<prefix>agent:{id}`` value (unix-epoch-seconds string) to an int.

    The redis client may hand back ``bytes`` or ``str`` depending on its decode setting;
    a malformed value (never written by Auth) is treated as absent so a parse glitch can
    never falsely revoke every still-valid token for an agent.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        logger.warning("revocation_agent_epoch_unparseable", value=repr(value))
        return None
