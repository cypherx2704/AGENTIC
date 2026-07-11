"""The graph store.

Callers use the :class:`GraphStore` port + the :func:`open_store` factory and
never name or import a concrete backend. Exactly one module
(:mod:`bkg.store.sqlite_store`) imports a database driver.
"""

from __future__ import annotations

from os import PathLike

from .base import Dep, GraphStore, InputRow, MemoRow
from .sqlite_store import SqliteGraphStore


def open_store(path: str | PathLike[str]) -> GraphStore:
    """Open the local graph store. Use ``":memory:"`` for an ephemeral store."""
    return SqliteGraphStore(path)


__all__ = ["GraphStore", "MemoRow", "Dep", "InputRow", "open_store"]
