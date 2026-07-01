"""Valkey (Redis) client — SOFT dependency.

Used for the shared verifier-side live-revocation MIRROR only (Contract 1 / WP03). A lazy
async client whose ``ping`` is soft-reported by ``/readyz`` (never gates readiness) and
whose ``revocation_lookup`` reads the SAME kill-switch keys Auth + the other verifiers
write. Every call FAILS OPEN — a missing/erroring Valkey accepts the token (availability
wins). cypherx-a1 keeps no other Valkey state in the MVP.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ValkeyClient:
    """Lazy async Valkey client (redis.asyncio) with fail-open helpers."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Any | None = None

    def _ensure(self) -> Any | None:
        if self._client is None:
            try:
                import redis.asyncio as redis

                self._client = redis.from_url(self._url, decode_responses=True)
            except Exception as exc:  # noqa: BLE001 — Valkey is soft; never crash on import/connect
                logger.warning("valkey_init_failed", error=str(exc))
                return None
        return self._client

    async def ping(self) -> bool:
        client = self._ensure()
        if client is None:
            return False
        try:
            return bool(await client.ping())
        except Exception:  # noqa: BLE001 — soft dependency
            return False

    async def revocation_lookup(
        self,
        *,
        prefix: str,
        jti: str | None,
        kid: str | None,
        agent_id: str | None,
        iat: int | None,
        timeout_seconds: float,
    ) -> bool:
        """Return True iff the token is revoked by jti, signing-kid, or agent-epoch.

        Reads ``<prefix>jti:{jti}``, ``<prefix>kid:{kid}`` (presence = revoked) and
        ``<prefix>agent:{agent_id}`` (an epoch newer than the token's ``iat`` = revoked).
        Raises on a Valkey error so the caller can FAIL OPEN.
        """
        client = self._ensure()
        if client is None:
            raise RuntimeError("valkey_unavailable")

        async def _lookup() -> bool:
            if jti and await client.exists(f"{prefix}jti:{jti}"):
                return True
            if kid and await client.exists(f"{prefix}kid:{kid}"):
                return True
            if agent_id is not None and iat is not None:
                epoch_raw = await client.get(f"{prefix}agent:{agent_id}")
                if epoch_raw is not None and int(epoch_raw) > iat:
                    return True
            return False

        return await asyncio.wait_for(_lookup(), timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — best-effort
                pass
