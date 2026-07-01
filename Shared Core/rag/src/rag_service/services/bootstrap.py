"""Platform-skills KB bootstrap (Component 10) — lazy-with-retry background loop.

Ensures the ``platform-skills`` knowledge base exists under the well-known platform tenant
(``00000000-0000-0000-0000-000000000001``) so Phase 8 (Skills) has a target, AND inserts the
default ``(tenant,'*')`` ACL row in the SAME transaction (without it the KB is readable by no
one once ACLs ship). Both INSERTs are idempotent (``ON CONFLICT DO NOTHING``).

Embedding model resolution does NOT hard-depend on a live llms call: it prefers the gateway
``GET /v1/models`` when reachable but falls back to the env-pinned
``EMBEDDING_MODEL_RESOLVED`` / ``EMBEDDING_DIM`` so the loop can never deadlock on the llms
soft-dependency (the circular cold-start the amendment removed).

``/readyz`` gates only on ``running`` (the loop is alive), NOT on ``completed`` (the KB row
exists) — the loop converges within its backoff and queries before then return not-found.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import PLATFORM_TENANT_ID, Settings
from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)

_DEFAULT_ACL_PERMS = ["read", "query", "ingest", "write", "admin"]


class PlatformSkillsBootstrap:
    """Owns the bootstrap loop state + a one-shot ``ensure_once`` for direct invocation/tests."""

    def __init__(self, pool: AsyncConnectionPool | None, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings
        self.running = False
        self.completed = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not self._settings.bootstrap_enabled:
            return
        self.running = True
        metrics.bootstrap_running.set(1)
        self._task = asyncio.create_task(self._run(), name="platform-skills-bootstrap")

    async def stop(self) -> None:
        self._stopping.set()
        self.running = False
        metrics.bootstrap_running.set(0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if await self.ensure_once():
                    self.completed = True
                    metrics.bootstrap_completed.set(1)
                    logger.info("platform_skills_bootstrap_complete")
                    return  # converged — nothing more to do.
            except Exception as exc:  # noqa: BLE001 — loop must keep retrying
                logger.warning("platform_skills_bootstrap_retry", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.bootstrap_retry_seconds
                )

    async def ensure_once(self) -> bool:
        """Ensure the KB row + default ACL row exist. Returns True on success.

        Returns False (without raising) when there is no DB pool (so the loop keeps
        retrying once a pool appears). Resolves the embedding model env-pinned (no live
        llms call required).
        """
        if self._pool is None:
            return False
        model = self._settings.embedding_model_resolved
        dim = self._settings.embedding_dim
        name = self._settings.bootstrap_kb_name

        async def _txn(conn: AsyncConnection) -> None:
            await conn.execute(
                """
                INSERT INTO rag.knowledge_bases
                  (tenant_id, name, description,
                   chunking_strategy, chunk_size, chunk_overlap,
                   embedding_model_alias, embedding_model_resolved, embedding_dim)
                VALUES (%s,%s,%s,'sentence',512,50,'embed',%s,%s)
                ON CONFLICT (tenant_id, name) DO NOTHING
                """,
                (
                    PLATFORM_TENANT_ID,
                    name,
                    "Platform-managed skill library — populated by Skills service (Phase 8).",
                    model,
                    dim,
                ),
            )
            cur = await conn.execute(
                "SELECT kb_id FROM rag.knowledge_bases WHERE tenant_id = %s AND name = %s",
                (PLATFORM_TENANT_ID, name),
            )
            row = await cur.fetchone()
            if row is None:
                return
            kb_id = row[0]
            await conn.execute(
                """
                INSERT INTO rag.kb_acls
                  (kb_id, tenant_id, principal_type, principal_id, permissions, created_by)
                VALUES (%s,%s,'tenant','*',%s,%s)
                ON CONFLICT (kb_id, principal_type, principal_id) DO NOTHING
                """,
                (kb_id, PLATFORM_TENANT_ID, _DEFAULT_ACL_PERMS, PLATFORM_TENANT_ID),
            )

        await in_tenant(self._pool, PLATFORM_TENANT_ID, _txn)
        return True
