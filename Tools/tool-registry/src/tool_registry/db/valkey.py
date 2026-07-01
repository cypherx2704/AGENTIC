"""Lazy async Valkey client (redis.asyncio) — soft dependency + WP03 revocation reads.

* ``GET /readyz`` reports ``valkey: "ok" | "unavailable"`` as a SOFT dependency —
  Valkey state never fails readiness (Contract 7).
* The ``tool_registry_valkey_up`` gauge tracks connectivity for alerting.
* ``get`` is used by the WP03 verifier-side revocation mirror; it RAISES on
  connect/timeout failure so the caller owns the fail-open decision. Only
  :meth:`ping` swallows errors.

The underlying ``redis.asyncio`` client is created lazily on first use (no TCP
connect at construction), so importing/booting the service never blocks on Valkey.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ..core import metrics

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
        """Return True if Valkey answers PING; update the up-gauge either way."""
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

        Bounded by ``timeout_seconds`` (defaults to the ping timeout). RAISES on
        connect/timeout failure so the revocation mirror can distinguish "key absent"
        (``None``) from "Valkey unavailable" and fail open.
        """
        client = self._ensure_client()
        bound = timeout_seconds if timeout_seconds is not None else self._ping_timeout
        raw = await asyncio.wait_for(client.get(key), timeout=bound)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)

    async def close(self) -> None:
        """Close the underlying client if it was ever created."""
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning("valkey_close_failed", error=str(exc))
        self._client = None
