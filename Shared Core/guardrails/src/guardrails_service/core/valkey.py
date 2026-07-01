"""Lazy async Valkey client wiring (WP02 cross-service foundation).

Valkey is a SOFT dependency (phase doc readiness rules): ``/readyz`` reports its
reachability but NEVER fails on it, and the ``guardrails_valkey_up`` gauge tracks the
last ping result. The connection URL and socket timeouts come from Settings
(``VALKEY_URL`` / ``VALKEY_TIMEOUT_SECONDS``) — nothing hardcoded.

No feature uses Valkey yet — the policy cache / rate limiting land in their own work
packages. This module is the shared client foundation only (same pattern as the llms
gateway). The client is created lazily on first use so the service boots (and unit
tests run) without a reachable Valkey.
"""

from __future__ import annotations

import asyncio

import structlog
from redis.asyncio import Redis

from . import metrics

logger = structlog.get_logger(__name__)


class ValkeyClient:
    """Lazily-connected async Valkey client (fail-soft on every operation)."""

    def __init__(
        self, url: str, *, timeout_seconds: float = 2.0, client: Redis | None = None
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        # Injection seam for tests (a fake Redis); production code leaves it None
        # and the real client is built lazily on first use.
        self._client = client

    def _ensure_client(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(
                self._url,
                socket_connect_timeout=self._timeout,
                socket_timeout=self._timeout,
                decode_responses=True,
            )
        return self._client

    async def ping(self) -> bool:
        """Return True if Valkey answers PING; updates the ``valkey_up`` gauge.

        Never raises — Valkey is a soft dependency and readiness/metrics callers
        must not 500 because the cache is down.
        """
        try:
            await self._ensure_client().ping()
        except Exception as exc:  # noqa: BLE001 — soft dependency; report, never raise
            logger.warning("valkey_ping_failed", error=str(exc))
            metrics.valkey_up.set(0)
            return False
        metrics.valkey_up.set(1)
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        """GET a key, returning its value or ``None`` on a genuine miss.

        Unlike :meth:`ping`, this does NOT swallow connection errors: callers that need
        to distinguish "key absent" (a real miss) from "Valkey unavailable" (so they can
        fail-open) must catch the raised exception. An optional ``timeout_seconds`` bounds
        the whole call independently of the client's socket timeout (the hot-path
        revocation check uses a much shorter budget than the readiness ping) — a timeout
        raises :class:`asyncio.TimeoutError`, which callers treat as "unavailable".
        """
        coro = self._ensure_client().get(key)
        raw = await (coro if timeout_seconds is None else asyncio.wait_for(coro, timeout_seconds))
        # The client is built with decode_responses=True, so values come back as ``str``;
        # decode defensively (redis-py's type is the str|bytes union) and pass ``None`` through.
        if isinstance(raw, bytes):
            return raw.decode()
        return raw

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """SET a key (optionally with a TTL via SETEX).

        Like :meth:`get`, this does NOT swallow connection errors — callers that treat
        Valkey as a best-effort cache catch the raised exception and continue. An optional
        ``timeout_seconds`` bounds the whole call independently of the socket timeout; a
        timeout raises :class:`asyncio.TimeoutError`.
        """
        client = self._ensure_client()
        if ttl_seconds is not None and ttl_seconds > 0:
            coro = client.set(key, value, ex=ttl_seconds)
        else:
            coro = client.set(key, value)
        await (coro if timeout_seconds is None else asyncio.wait_for(coro, timeout_seconds))

    async def eval(
        self,
        script: str,
        *,
        keys: list[str],
        args: list[str | int],
        timeout_seconds: float | None = None,
    ) -> object:
        """Run a Lua script atomically (EVAL). Raises on a Valkey error/timeout.

        Used by the rate limiter for a single round-trip atomic INCR-with-window so two
        concurrent checks cannot both slip past the cap. Callers decide the fail posture
        on the raised exception (the limiter fails CLOSED unless configured otherwise).
        """
        client = self._ensure_client()
        coro = client.eval(script, len(keys), *keys, *args)
        return await (coro if timeout_seconds is None else asyncio.wait_for(coro, timeout_seconds))

    async def aclose(self) -> None:
        """Close the underlying connection pool (no-op if never connected)."""
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("valkey_close_failed", error=str(exc))
