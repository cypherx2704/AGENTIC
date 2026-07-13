"""SQLite implementation of the ``GraphStore`` port.

This is the ONLY module in bkg that imports a database driver. Everything else
depends on :class:`bkg.store.base.GraphStore` and the ``open_store`` factory, so
a future backend swap touches nothing but this file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from os import PathLike

from .base import Dep, GraphStore, InputRow, MemoRow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memo (
    key          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    value        BLOB NOT NULL,
    value_fp     BLOB NOT NULL,
    changed_rev  INTEGER NOT NULL DEFAULT 0,
    verified_rev INTEGER NOT NULL DEFAULT 0,
    provenance   TEXT NOT NULL DEFAULT 'static',
    confidence   TEXT NOT NULL DEFAULT 'static-certain',
    partial      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deps (
    node_key       TEXT NOT NULL,
    dep_key        TEXT NOT NULL,
    dep_fp_at_read BLOB NOT NULL,
    PRIMARY KEY (node_key, dep_key)
);
CREATE INDEX IF NOT EXISTS rdeps ON deps (dep_key);
CREATE TABLE IF NOT EXISTS inputs (
    input_id    TEXT PRIMARY KEY,
    content_fp  BLOB NOT NULL,
    changed_rev INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL
);
"""

_MEMO_COLS = (
    "key, kind, value, value_fp, changed_rev, verified_rev, provenance, confidence, partial"
)


def _to_memo(row: tuple) -> MemoRow:
    return MemoRow(
        key=row[0],
        kind=row[1],
        value=bytes(row[2]),
        value_fp=bytes(row[3]),
        changed_rev=row[4],
        verified_rev=row[5],
        provenance=row[6],
        confidence=row[7],
        partial=bool(row[8]),
    )


class SqliteGraphStore(GraphStore):
    def __init__(self, path: str | PathLike[str]) -> None:
        # check_same_thread=False: the connection may be created in one thread and
        # served from another (the HTTP API runs sync handlers in a threadpool). The
        # single-owner design guarantees serialized access — the CLI/MCP/daemon are
        # single-threaded and the API holds a per-request lock — so cross-thread use
        # never overlaps. sqlite3 otherwise pins a connection to its creating thread.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # If another connection briefly holds the write lock, wait instead of
        # failing immediately. The single-owner design (one Daemon per project)
        # means real contention is rare; this only smooths accidental overlap.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('global_revision', 0)")
        self._conn.commit()

    # --- global revision ---------------------------------------------------
    def get_revision(self) -> int:
        row = self._conn.execute("SELECT v FROM meta WHERE k = 'global_revision'").fetchone()
        return int(row[0])

    def bump_revision(self) -> int:
        # No commit here: the caller's transaction() (or close()) commits, so a
        # revision bump is atomic with the writes that accompany it.
        self._conn.execute("UPDATE meta SET v = v + 1 WHERE k = 'global_revision'")
        return self.get_revision()

    # --- memo --------------------------------------------------------------
    def get_node(self, key: str) -> MemoRow | None:
        row = self._conn.execute(
            f"SELECT {_MEMO_COLS} FROM memo WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else _to_memo(row)

    def put_node(self, row: MemoRow) -> None:
        self._conn.execute(
            f"INSERT INTO memo ({_MEMO_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "kind=excluded.kind, value=excluded.value, value_fp=excluded.value_fp, "
            "changed_rev=excluded.changed_rev, verified_rev=excluded.verified_rev, "
            "provenance=excluded.provenance, confidence=excluded.confidence, partial=excluded.partial",
            (
                row.key,
                row.kind,
                row.value,
                row.value_fp,
                row.changed_rev,
                row.verified_rev,
                row.provenance,
                row.confidence,
                int(row.partial),
            ),
        )

    def delete_node(self, key: str) -> None:
        self._conn.execute("DELETE FROM memo WHERE key = ?", (key,))

    def all_nodes(self) -> Iterable[MemoRow]:
        for row in self._conn.execute(f"SELECT {_MEMO_COLS} FROM memo"):
            yield _to_memo(row)

    # --- deps / rdeps ------------------------------------------------------
    def get_deps(self, key: str) -> list[Dep]:
        return [
            Dep(node_key=r[0], dep_key=r[1], dep_fp_at_read=bytes(r[2]))
            for r in self._conn.execute(
                "SELECT node_key, dep_key, dep_fp_at_read FROM deps WHERE node_key = ?", (key,)
            )
        ]

    def put_deps(self, key: str, deps: list[Dep]) -> None:
        self._conn.execute("DELETE FROM deps WHERE node_key = ?", (key,))
        # node_key column is authoritative from `key`, not the Dep's own field.
        self._conn.executemany(
            "INSERT INTO deps (node_key, dep_key, dep_fp_at_read) VALUES (?, ?, ?)",
            [(key, d.dep_key, d.dep_fp_at_read) for d in deps],
        )

    def get_rdeps(self, dep_key: str) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT node_key FROM deps WHERE dep_key = ? ORDER BY node_key", (dep_key,)
            )
        ]

    # --- inputs ------------------------------------------------------------
    def get_input(self, input_id: str) -> InputRow | None:
        row = self._conn.execute(
            "SELECT input_id, content_fp, changed_rev FROM inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            return None
        return InputRow(input_id=row[0], content_fp=bytes(row[1]), changed_rev=row[2])

    def set_input(self, input_id: str, content_fp: bytes, changed_rev: int) -> None:
        self._conn.execute(
            "INSERT INTO inputs (input_id, content_fp, changed_rev) VALUES (?, ?, ?) "
            "ON CONFLICT(input_id) DO UPDATE SET "
            "content_fp=excluded.content_fp, changed_rev=excluded.changed_rev",
            (input_id, content_fp, changed_rev),
        )

    # --- lifecycle ---------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
