"""Lazy async Valkey client (redis.asyncio) — soft dependency.

Valkey backs only the fail-open per-tenant rate limiter and idempotency replay, and
is reported by ``GET /readyz`` as ``valkey: "ok" | "unavailable"`` (a SOFT dependency —
its state NEVER fails readiness). The ``tws_valkey_up`` gauge tracks connectivity.

Narrow async command helpers (``get`` / ``set`` / ``incr_with_expire``) RAISE on
connect/timeout failure so the CALLER owns the fail-open decision (the rate limiter and
idempotency both fail open); only :meth:`ping` swallows errors.

The underlying ``redis.asyncio`` client is created lazily on first use (no TCP connect
at construction), so importing/booting the server never blocks on Valkey.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from . import metrics

logger = structlog.get_logger(__name__)


class ValkeyClient:
    """Lazily-connected redis.asyncio wrapper with fail-soft ``ping``."""

    def __init__(self, url: str, *, ping_timeout: float = 2.0) -> None:
        self._url = url
        self._ping_timeout = ping_timeout
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        """Create the redis.asyncio client on first use (connects per-command)."""
        if self._client is None:
            import redis.asyncio as redis  # local import: only loaded when needed

            self._client = redis.Redis.from_url(self._url)
        return self._client

    async def ping(self) -> bool:
        """Return True if Valkey answers PING; update ``tws_valkey_up`` either way."""
        try:
            client = self._ensure_client()
            await asyncio.wait_for(client.ping(), timeout=self._ping_timeout)
        except Exception as exc:  # noqa: BLE001 — soft dependency must never raise
            logger.warning("valkey_ping_failed", error=str(exc))
            metrics.valkey_up.set(0)
            return False
        metrics.valkey_up.set(1)
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        """GET a key, decoded to ``str``; ``None`` when the key is absent.

        Bounded by ``timeout_seconds`` (defaults to the ping timeout). Unlike
        :meth:`ping`, this RAISES on connect/timeout failure: callers own the fail-open
        decision and must distinguish "key absent" (``None``) from "Valkey unavailable".
        """
        client = self._ensure_client()
        bound = timeout_seconds if timeout_seconds is not None else self._ping_timeout
        raw = await asyncio.wait_for(client.get(key), timeout=bound)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)

    async def incr_with_expire(
        self,
        key: str,
        *,
        ttl_seconds: int,
        timeout_seconds: float | None = None,
    ) -> int:
        """Atomically ``INCR`` a counter and set its TTL on first creation; return the
        new value.

        Used by the fixed-window rate limiter. The INCR + EXPIRE are issued in a single
        pipeline/transaction so the window key always gets a TTL even under a race.
        RAISES on connect/timeout failure: the caller (rate limiter) owns the fail-open
        decision.
        """
        client = self._ensure_client()
        bound = timeout_seconds if timeout_seconds is not None else self._ping_timeout

        async def _run() -> int:
            pipe = client.pipeline(transaction=True)
            pipe.incr(key)
            pipe.expire(key, ttl_seconds)
            results = await pipe.execute()
            return int(results[0])

        return await asyncio.wait_for(_run(), timeout=bound)

    async def set_if_absent(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int,
        timeout_seconds: float | None = None,
    ) -> bool:
        """``SET key value NX EX ttl`` — return True if the key was set (did not exist),
        False if it already existed. RAISES on connect/timeout failure (caller owns
        fail-open)."""
        client = self._ensure_client()
        bound = timeout_seconds if timeout_seconds is not None else self._ping_timeout
        result = await asyncio.wait_for(
            client.set(key, value, nx=True, ex=ttl_seconds), timeout=bound
        )
        return bool(result)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """``SET key value [EX ttl]`` (unconditional overwrite). RAISES on
        connect/timeout failure."""
        client = self._ensure_client()
        bound = timeout_seconds if timeout_seconds is not None else self._ping_timeout
        await asyncio.wait_for(client.set(key, value, ex=ttl_seconds), timeout=bound)

    async def close(self) -> None:
        """Close the underlying client if it was ever created."""
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning("valkey_close_failed", error=str(exc))
        self._client = None
