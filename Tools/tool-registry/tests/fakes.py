"""Test fakes — a scriptable psycopg AsyncConnectionPool + a fake manifest HTTP client.

The fake pool duck-types exactly the surface ``db.queries`` exercises through
``in_tenant`` / ``in_platform``:

    pool.connection()  -> async-CM yielding conn
    conn.transaction() -> async-CM yielding conn
    conn.execute(sql, params)                          -> awaitable (returns a cursor)
    conn.cursor(row_factory=...).execute(sql, params)  -> awaitable cursor
    cursor.fetchall() / cursor.fetchone()

``set_config('app.tenant_id', ...)`` calls are captured into ``last_tenant`` so a test
can prove the tenant came from the Principal, not a body/query param. SELECTs are
matched against a small ordered list of canned ``(predicate, rows)`` responders; the
first matching responder pops/returns its rows. INSERT/UPDATE/DELETE statements are
appended to ``writes`` so retention/registration behaviour can be asserted, and an
optional ``write_hook`` lets a test simulate a constraint violation (e.g. the RLS
WITH CHECK rejection) by raising.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Responder:
    def __init__(self, predicate: Callable[[str], bool], rows: list[dict[str, Any]], *, once: bool):
        self.predicate = predicate
        self.rows = rows
        self.once = once
        self.used = False


class FakeConn:
    def __init__(self, owner: FakePool) -> None:
        self._owner = owner

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    def _match(self, sql: str) -> list[dict[str, Any]]:
        for r in self._owner.responders:
            if r.once and r.used:
                continue
            if r.predicate(sql):
                r.used = True
                return r.rows
        return []

    async def execute(self, sql: str, params: Any = None) -> FakeCursor:
        norm = " ".join(sql.split())
        if "set_config" in norm:
            # in_tenant passes the tenant as a param; in_platform inlines '' (no params).
            self._owner.last_tenant = params[0] if params else ""
            return FakeCursor([])
        upper = norm.lstrip().upper()
        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            self._owner.writes.append((norm, params))
            if self._owner.write_hook is not None:
                self._owner.write_hook(norm, params)
            return FakeCursor([])
        return FakeCursor(self._match(norm))

    def cursor(self, *, row_factory: Any = None) -> _CursorBuilder:
        return _CursorBuilder(self)


class _CursorBuilder:
    """Mirror of ``conn.cursor(row_factory=...).execute(...)`` returning a FakeCursor."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Any = None) -> FakeCursor:
        norm = " ".join(sql.split())
        upper = norm.lstrip().upper()
        if upper.startswith("INSERT") and "RETURNING" in upper:
            # Capture the write, then return the canned RETURNING row.
            self._conn._owner.writes.append((norm, params))
            if self._conn._owner.write_hook is not None:
                self._conn._owner.write_hook(norm, params)
            return FakeCursor(self._conn._match(norm))
        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            self._conn._owner.writes.append((norm, params))
            if self._conn._owner.write_hook is not None:
                self._conn._owner.write_hook(norm, params)
            return FakeCursor([])
        return FakeCursor(self._conn._match(norm))


class FakePool:
    """Scriptable stand-in for psycopg's AsyncConnectionPool."""

    def __init__(self) -> None:
        self.responders: list[_Responder] = []
        self.writes: list[tuple[str, Any]] = []
        self.last_tenant: str | None = None
        self.write_hook: Callable[[str, Any], None] | None = None

    def on(self, contains: str, rows: list[dict[str, Any]], *, once: bool = False) -> FakePool:
        """Register a SELECT responder: when the normalised SQL contains ``contains``,
        return ``rows``. ``once`` consumes the responder after a single match."""
        token = contains
        self.responders.append(_Responder(lambda sql, t=token: t in sql, rows, once=once))
        return self

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield FakeConn(self)


# ── Fake manifest HTTP client (drives 200 / 304 / error sequences) ────────────────
class FakeResponse:
    def __init__(self, status_code: int, *, etag: str | None = None, body: Any = None) -> None:
        self.status_code = status_code
        self.headers = {"ETag": etag} if etag else {}
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttpClient:
    """Returns a scripted sequence of responses (or raises) per ``get`` call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def get(
        self, url: str, *, headers: dict[str, str],
        timeout: float,  # noqa: ASYNC109 — duck-types the HttpClient protocol (httpx-style)
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if not self._script:
            raise AssertionError("FakeHttpClient ran out of scripted responses")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
