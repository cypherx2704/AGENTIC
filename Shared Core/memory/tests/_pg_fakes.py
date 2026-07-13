"""A minimal recording psycopg-shaped fake so PG-repo SQL emission is testable offline.

The app suite runs against the InMemoryRepository (conftest NULLs db_pool), which executes
NO SQL — so it cannot see the B1 halfvec cast, the B3 ``SET LOCAL hnsw.ef_search`` GUC, the
B6 conditional embedding fetch, or the B7 link SQL. These features build their SQL
DYNAMICALLY (f-strings keyed on config), so ``inspect.getsource`` can't see the emitted text
either. This fake records every executed statement so a test can assert the exact SQL the
repo sends, while returning caller-supplied rows so the Python-side ranking still runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class _ACM:
    """Trivial async context manager yielding a fixed value (pool.connection / transaction)."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _ExecResult:
    rowcount = 0

    async def fetchone(self) -> None:
        return None


class RecordingCursor:
    def __init__(self, conn: RecordingConn, rows: list[dict[str, Any]]) -> None:
        self._conn = conn
        self._rows = rows

    async def execute(self, sql: str, params: Any = None) -> RecordingCursor:
        self._conn.executed.append((sql, params))
        return self

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class RecordingConn:
    """Records executed SQL; hands successive ``cursor()`` queries the next queued row-set."""

    def __init__(self, cursor_rows: list[list[dict[str, Any]]] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._cursor_rows = list(cursor_rows or [])

    async def execute(self, sql: str, params: Any = None) -> _ExecResult:
        self.executed.append((sql, params))
        return _ExecResult()

    def cursor(self, row_factory: Any = None) -> RecordingCursor:
        rows = self._cursor_rows.pop(0) if self._cursor_rows else []
        return RecordingCursor(self, rows)

    def transaction(self) -> _ACM:
        return _ACM(None)

    @property
    def sql(self) -> str:
        """All executed SQL concatenated (for substring assertions)."""
        return "\n".join(s for s, _ in self.executed)


class RecordingPool:
    def __init__(self, conn: RecordingConn) -> None:
        self._conn = conn

    def connection(self, *args: object, **kwargs: object) -> _ACM:
        return _ACM(self._conn)


def full_row(**overrides: Any) -> dict[str, Any]:
    """A complete memory.memories dict_row (every column _row_to_memory reads), overridable."""
    now = datetime.now(UTC)
    row: dict[str, Any] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "00000000-0000-0000-0000-0000000000aa",
        "principal_type": "agent",
        "principal_id": "agent-aaaa",
        "scope": "principal_only",
        "type": "note",
        "tags": [],
        "content": "hello world",
        "metadata": {},
        "session_id": None,
        "score": 1.0,
        "created_at": now,
        "last_accessed_at": now,
        "expires_at": None,
        "importance_score": 0.5,
        "last_retrieved_at": None,
        "valid_until": None,
        "superseded_by_id": None,
        "session_scope_id": None,
        "agent_scope_id": None,
        "access_count": 0,
        "similarity": 0.9,
    }
    row.update(overrides)
    return row
