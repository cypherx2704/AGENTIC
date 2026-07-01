"""Deterministic in-memory psycopg fakes for the WP07 DB-backed endpoints (no real DB).

The WP07 handlers all reach the database through the same narrow seam:

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        cur = await conn.cursor(row_factory=...).execute(SQL, params)
        rows = await cur.fetchone() / .fetchall()
        await conn.execute(SQL, params)   # rowcount on UPDATE/DELETE

:class:`ScriptedPool` stands in for ``AsyncConnectionPool``: it records EVERY executed
``(sql, params)`` (so a test can assert the writes that landed) and answers reads from a
caller-supplied ``responder`` keyed on a substring of the SQL. This is enough to exercise
the policy/rules/violations/redaction-key handlers (and ``record_*`` outbox writes) without
psycopg or Postgres, mirroring the ``_RecordingPool`` style already used in the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# A responder maps an executed (sql, params) to the rows a cursor should return.
# Return a list[tuple] for fetchall / the first tuple for fetchone; None => no row.
Responder = Callable[[str, Any], list[tuple[Any, ...]] | None]


class _NullTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _ScriptedCursor:
    def __init__(self, conn: _ScriptedConn) -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    async def execute(self, query: str, params: Any = None) -> _ScriptedCursor:
        self._rows = self._conn._record_and_resolve(str(query), params)
        return self

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _ScriptedConn:
    def __init__(self, pool: ScriptedPool) -> None:
        self._pool = pool
        self.rowcount = 0

    def _record_and_resolve(self, query: str, params: Any) -> list[tuple[Any, ...]]:
        self._pool.executed.append((query, params))
        rows = self._pool.responder(query, params) if self._pool.responder else None
        # rowcount mirrors the resolved row count so UPDATE/DELETE RETURNING-less paths work.
        self.rowcount = len(rows) if rows is not None else self._pool.default_rowcount
        return rows or []

    def cursor(self, row_factory: Any = None) -> _ScriptedCursor:
        return _ScriptedCursor(self)

    async def execute(self, query: str, params: Any = None) -> _ScriptedConn:
        self._record_and_resolve(str(query), params)
        return self

    def transaction(self) -> _NullTxn:
        return _NullTxn()


class _ScriptedConnCtx:
    def __init__(self, conn: _ScriptedConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _ScriptedConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class ScriptedPool:
    """Quacks like ``AsyncConnectionPool`` for the WP07 read/write seam.

    * ``executed`` — every ``(sql, params)`` run on any connection, in order.
    * ``responder`` — ``(sql, params) -> rows | None`` to answer reads (RETURNING / SELECT).
    * ``default_rowcount`` — rowcount reported for a write the responder did not script
      (e.g. a successful UPDATE/DELETE); set to 0 to simulate "no row matched".
    """

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        default_rowcount: int = 1,
    ) -> None:
        self.responder = responder
        self.default_rowcount = default_rowcount
        self.executed: list[tuple[str, Any]] = []
        self.connections = 0

    def connection(self, timeout: float | None = None) -> _ScriptedConnCtx:
        self.connections += 1
        return _ScriptedConnCtx(_ScriptedConn(self))

    # ── Assertion helpers ─────────────────────────────────────────────────────────
    def find(self, needle: str) -> list[tuple[str, Any]]:
        """All executed statements whose SQL contains ``needle``."""
        return [(q, p) for q, p in self.executed if needle in q]

    def ran(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.executed)


class FailingPool:
    """A pool whose connection acquisition always fails (DB down)."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("db down")

    def connection(self, timeout: float | None = None) -> Any:
        raise self._exc
