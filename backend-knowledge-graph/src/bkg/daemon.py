"""Long-lived daemon: watches a project directory and keeps the graph in sync
**incrementally** — each file change recomputes only the affected facts. This is
what makes the incremental engine actually incremental in use (the CLI rebuilds
from scratch per invocation; the daemon updates in place).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

from .service import _SKIP_DIRS, GraphService
from .store import open_store

# a file's change-signature: (mtime, size). Size is the tiebreaker so a
# content edit that PRESERVES mtime (git checkout, tar --times, coarse fs tick)
# is still detected. None = stat failed -> force a re-read but keep the file.
_Sig = tuple[float, int] | None

_DB_DIR = ".bkg"
_DB_FILE = "graph.db"


def default_db_path(root: str) -> str:
    """The persistent graph lives at ``<root>/.bkg/graph.db`` (like ``.git``).
    Creates the dir and a self-ignoring ``.bkg/.gitignore`` so the machine-local
    DB is never committed. The DB is a rebuildable cache, not source of truth."""
    bkg = os.path.join(root, _DB_DIR)
    os.makedirs(bkg, exist_ok=True)
    ignore = os.path.join(bkg, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("*\n")
    return os.path.join(bkg, _DB_FILE)


class Daemon:
    def __init__(self, root: str, db_path: str | None = None) -> None:
        """Long-lived, SINGLE-owner graph for one project. By default it persists
        to ``<root>/.bkg/graph.db`` so a restart reopens the graph warm instead of
        rebuilding it. Pass ``db_path=":memory:"`` for an ephemeral daemon."""
        self.root = root
        path = db_path if db_path is not None else default_db_path(root)
        self.service = GraphService(store=open_store(path))
        self._sigs: dict[str, _Sig] = {}
        # initial load goes through the SAME mtime-aware path, so the signature of
        # each file is captured no later than its content read (no stale-pin race).
        # On a warm store, unchanged files re-read but recompute nothing (early cutoff).
        self.resync()

    def _rel(self, abspath: str) -> str:
        return os.path.relpath(abspath, self.root).replace(os.sep, "/")

    def _sig(self, abspath: str) -> _Sig:
        try:
            st = os.stat(abspath)
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def _scan(self) -> list[tuple[str, str, _Sig]]:
        """Every ``.py`` file under root as ``(rel, abspath, sig)`` (skips noise dirs).
        A file whose stat fails is still listed (sig=None) so it is never reaped."""
        found: list[tuple[str, str, _Sig]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    abspath = os.path.join(dirpath, name)
                    found.append((self._rel(abspath), abspath, self._sig(abspath)))
        return found

    def apply_events(self, events: Iterable[tuple[str, str]]) -> None:
        """Apply filesystem events — each ``(kind, abspath)`` with kind in
        {"add", "modify", "delete"}. Non-``.py`` paths are ignored."""
        for kind, abspath in events:
            if not abspath.endswith(".py"):
                continue
            rel = self._rel(abspath)
            if kind == "delete":
                self.service.remove_file(rel)
                self._sigs.pop(rel, None)
                continue
            try:
                with open(abspath, encoding="utf-8-sig") as handle:
                    text = handle.read()
            except OSError:  # created-then-deleted race -> treat as removed
                self.service.remove_file(rel)
                self._sigs.pop(rel, None)
                continue
            self.service.update_file(rel, text)
            self._sigs[rel] = self._sig(abspath)

    def resync(self) -> None:
        """Reconcile the graph against the current on-disk state, re-reading only
        files whose (mtime, size) changed (cheap enough to call before every MCP
        query). A present-but-unreadable file keeps its prior facts, is never
        reaped, and is retried on the next resync."""
        seen: set[str] = set()
        for rel, abspath, sig in self._scan():
            seen.add(rel)  # listed on disk -> never reaped, even if stat/read failed
            if sig is not None and self._sigs.get(rel) == sig:
                continue  # unchanged -> skip the read entirely
            try:
                with open(abspath, encoding="utf-8-sig") as handle:
                    text = handle.read()
            except OSError:
                continue  # transient lock/permission: keep prior facts, retry next resync
            self.service.update_file(rel, text)
            self._sigs[rel] = sig
        for gone in sorted(self.service.files() - seen):
            self.service.remove_file(gone)
            self._sigs.pop(gone, None)

    def watch(self, on_change: Callable[[GraphService], None] | None = None) -> None:  # pragma: no cover
        """Block, applying real-time file changes as they happen."""
        from watchfiles import Change, watch

        kinds = {Change.added: "add", Change.modified: "modify", Change.deleted: "delete"}
        for changes in watch(self.root):
            self.apply_events([(kinds[change], path) for change, path in changes])
            if on_change is not None:
                on_change(self.service)
