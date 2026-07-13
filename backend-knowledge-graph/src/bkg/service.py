"""GraphService — the RPC surface shared by the CLI and the MCP server.

Holds a long-lived incremental engine over a project's source and answers queries
with pure index lookups (**no LLM on the query path**). The CLI and the MCP
server are thin transports over these methods — which is the proof the graph, not
the transport, is the product.
"""

from __future__ import annotations

import os
from typing import Any

from .ai import AiAnalysisProvider, AiCache, HeuristicProvider, propose_for_endpoints
from .engine import Engine
from .pipeline import ROOT, install
from .runtime import reconcile, to_observations
from .store import GraphStore, open_store

_SKIP_DIRS = frozenset(
    {
        "__pycache__", ".git", ".venv", "venv", "node_modules",
        ".mypy_cache", ".ruff_cache", ".pytest_cache", ".bkg",
    }
)


def _match_search(ep: dict[str, Any], q: str) -> bool:
    """Case-insensitive substring match over method, resolved path, handler, and tags
    (``q`` must already be lower-cased and stripped)."""
    haystack = " ".join([ep["method"], ep["resolved_path"], ep["handler"], *ep.get("tags", [])])
    return q in haystack.lower()


def _match_method(ep: dict[str, Any], method: str) -> bool:
    """Exact HTTP-method match (``method`` must already be upper-cased)."""
    return bool(ep["method"] == method)


def _match_tag(ep: dict[str, Any], tag: str) -> bool:
    """Exact case-insensitive tag membership (``tag`` must already be lower-cased)."""
    return any(tag == t.lower() for t in ep.get("tags", []))


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
        # Recover the manifest from a persisted (warm) store, so a file deleted
        # while the service was down is still reaped on the next resync. Empty for
        # a fresh store. from_sources overwrites this with its exact source set.
        self._files: set[str] = self._recover_manifest()

    def _recover_manifest(self) -> set[str]:
        try:
            return set(self._engine.query("files:all"))
        except KeyError:
            return set()  # fresh store — no manifest persisted yet

    @classmethod
    def from_sources(cls, sources: dict[str, str], store: GraphStore | None = None) -> GraphService:
        service = cls(store)
        service._files = set(sources)
        service._engine.set_input("files:all", sorted(sources))
        for path in sorted(sources):
            service._engine.set_input(f"fileText:{path}", sources[path])
        return service

    @classmethod
    def from_directory(cls, root: str, store: GraphStore | None = None) -> GraphService:
        return cls.from_sources(load_directory(root), store)

    def files(self) -> set[str]:
        return set(self._files)

    def update_file(self, path: str, text: str) -> None:
        """Add or update one file's source; only affected facts recompute."""
        if path not in self._files:
            self._files.add(path)
            self._engine.set_input("files:all", sorted(self._files))
        self._engine.set_input(f"fileText:{path}", text)

    def remove_file(self, path: str) -> None:
        """Remove a file: drop it from the manifest first, then its input, so the
        reverse-dep closure is invalidated cleanly."""
        if path not in self._files:
            return
        self._files.discard(path)
        self._engine.set_input("files:all", sorted(self._files))
        self._engine.remove_input(f"fileText:{path}")

    def apply_change(self, path: str, text: str) -> None:  # backwards-compatible alias
        self.update_file(path, text)

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

    def get_endpoint_by_id(self, endpoint_id: str) -> dict[str, Any] | None:
        """Look up one endpoint by its opaque ``id`` (the stable key clients persist —
        the runner, generated tests, and the UI address endpoints by this)."""
        for ep in self.list_endpoints():
            if ep["id"] == endpoint_id:
                return ep
        return None

    # --- Endpoint Registry: search + filter (pure index lookups, no LLM) ---
    # NOTE: filter_by_repository is intentionally ABSENT — the graph has no repository
    # identity yet (endpoints are keyed by repo-relative file path only). It arrives
    # with cross-repository support (roadmap Phase 3).

    def search_endpoints(self, query: str) -> list[dict[str, Any]]:
        """Endpoints whose method / resolved path / handler / tags contain ``query``
        (case-insensitive substring). An empty or whitespace-only query returns every
        endpoint (no-filter semantics)."""
        q = query.strip().lower()
        endpoints = self.list_endpoints()
        if not q:
            return endpoints
        return [ep for ep in endpoints if _match_search(ep, q)]

    def filter_by_method(self, method: str) -> list[dict[str, Any]]:
        """Endpoints with the given HTTP method (case-insensitive exact match)."""
        m = method.strip().upper()
        return [ep for ep in self.list_endpoints() if _match_method(ep, m)]

    def filter_by_tag(self, tag: str) -> list[dict[str, Any]]:
        """Endpoints carrying ``tag`` (case-insensitive exact match). Tags come from
        FastAPI route / ``include_router`` ``tags=[...]``."""
        t = tag.strip().lower()
        return [ep for ep in self.list_endpoints() if _match_tag(ep, t)]

    def list_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for path in self._engine.query("files:all"):
            for s in self._engine.query(f"schemaDeclList:{path}"):
                # project the served field shape; the node also carries internal
                # resolution candidates + IR metadata that must not leak to clients.
                fields = [
                    {"name": f["name"], "type": f["type"], "required": f["required"], "default": f["default"]}
                    for f in s["fields"]
                ]
                schemas.append(
                    {"id": f"{path}:{s['name']}", "name": s["name"], "file": path, "fields": fields}
                )
        return schemas

    def list_config(self) -> list[dict[str, Any]]:
        """Configuration surface — environment-variable reads (`os.getenv`/`os.environ`)
        and pydantic `BaseSettings` fields — the runner needs these for base URL / host
        / secrets. Deterministic, always `static-certain` (direct reads, no cross-file)."""
        config: list[dict[str, Any]] = []
        for path in self._engine.query("files:all"):
            for c in self._engine.query(f"configDeclList:{path}"):
                config.append(
                    {**c, "file": path, "source": "static", "confidence": "static-certain"}
                )
        return config

    def blast_radius(self, schema_id: str) -> list[str]:
        """Endpoint ids referencing the DTO ``schema_id`` (``file:Model``) as body
        or response — i.e. what to re-check if that DTO changes."""
        self._engine.query(ROOT)  # ensure the graph + dependency edges are built
        closure = self._engine.reverse_dependencies(f"schemaRef:{schema_id}")
        return sorted(k[len("endpoint:") :] for k in closure if k.startswith("endpoint:"))

    def trust_summary(self) -> dict[str, Any]:
        """How much of the served graph is certain vs inferred vs incomplete —
        the honest 'what do we actually know' report (all deterministic today)."""
        endpoints = self.list_endpoints()
        by_confidence: dict[str, int] = {}
        partial = 0
        for ep in endpoints:
            by_confidence[ep["confidence"]] = by_confidence.get(ep["confidence"], 0) + 1
            if ep.get("partial"):
                partial += 1
        return {"endpoints": len(endpoints), "by_confidence": by_confidence, "partial": partial}

    def propose_gaps(
        self,
        provider: AiAnalysisProvider | None = None,
        cache: AiCache | None = None,
    ) -> list[dict[str, Any]]:
        """Endpoints with AI proposals attached to gaps (absent/unresolved response
        DTOs). Proposals are tagged ``ai-inferred`` and kept in a SEPARATE
        ``ai_proposals`` field — the static facts are never modified. Opt-in; the
        deterministic graph and its query path are unaffected."""
        provider = provider or HeuristicProvider()
        cache = cache or AiCache()
        endpoints = self.list_endpoints()
        proposals = propose_for_endpoints(endpoints, provider, cache)
        enriched: list[dict[str, Any]] = []
        for ep in endpoints:
            item = dict(ep)
            if ep["id"] in proposals:
                item["ai_proposals"] = [p.to_dict() for p in proposals[ep["id"]]]
            enriched.append(item)
        return enriched

    def reconcile_runtime(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        """Reconcile observed traffic (``[{method, path, status?}, ...]``) with the
        static graph: static endpoints that were observed become
        ``runtime-confirmed``, and observed paths with no static match are surfaced
        as ``runtime_only`` (dynamic routes static analysis missed). Static facts
        are never modified — runtime only raises confidence."""
        endpoints, runtime_only = reconcile(self.list_endpoints(), to_observations(observations))
        confirmed = sum(1 for ep in endpoints if ep["verification_status"] == "runtime-confirmed")
        return {"endpoints": endpoints, "runtime_only": runtime_only, "confirmed": confirmed}

    def snapshot_digest(self) -> str:
        return self._engine.snapshot_digest(ROOT)
