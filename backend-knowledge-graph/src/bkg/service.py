"""GraphService — the RPC surface shared by the CLI and the MCP server.

Holds a long-lived incremental engine over a project's source and answers queries
with pure index lookups (**no LLM on the query path**). The CLI and the MCP
server are thin transports over these methods — which is the proof the graph, not
the transport, is the product.
"""

from __future__ import annotations

import os
from typing import Any

from .engine import Engine
from .pipeline import ROOT, apply_sources, install
from .store import GraphStore, open_store

_SKIP_DIRS = frozenset(
    {
        "__pycache__", ".git", ".venv", "venv", "node_modules",
        ".mypy_cache", ".ruff_cache", ".pytest_cache", ".bkg",
    }
)


def load_directory(root: str) -> dict[str, str]:
    """Read every ``.py`` file under ``root`` into ``{repo-relative POSIX path: source}``."""
    sources: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                # utf-8-sig strips a leading BOM (common on Windows-authored files)
                with open(full, encoding="utf-8-sig") as handle:
                    sources[rel] = handle.read()
    return sources


class GraphService:
    def __init__(self, store: GraphStore | None = None) -> None:
        self._engine = Engine(store if store is not None else open_store(":memory:"))
        install(self._engine)

    @classmethod
    def from_sources(cls, sources: dict[str, str], store: GraphStore | None = None) -> GraphService:
        service = cls(store)
        apply_sources(service._engine, sources)
        return service

    @classmethod
    def from_directory(cls, root: str, store: GraphStore | None = None) -> GraphService:
        return cls.from_sources(load_directory(root), store)

    def apply_change(self, path: str, text: str) -> None:
        """Incrementally update one file's source; only affected facts recompute."""
        self._engine.set_input(f"fileText:{path}", text)

    def list_endpoints(self) -> list[dict[str, Any]]:
        endpoints: list[dict[str, Any]] = []
        for key in self._engine.query(ROOT):
            ep = self._engine.query(key)
            if ep is None:
                continue
            endpoints.append({"id": key[len("endpoint:") :], **ep})
        return endpoints

    def get_endpoint(self, method: str, path: str) -> dict[str, Any] | None:
        method = method.upper()
        for ep in self.list_endpoints():
            if ep["method"] == method and ep["resolved_path"] == path:
                return ep
        return None

    def snapshot_digest(self) -> str:
        return self._engine.snapshot_digest(ROOT)
