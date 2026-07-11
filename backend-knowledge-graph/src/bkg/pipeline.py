"""The real backend-graph pipeline: memoized queries that turn Python source into
assembled Endpoints, feeding the incremental engine.

Firewall structure (each per-fact node depends on a PROJECTION, never raw text):

    fileText:{p}   (input: source)
      -> fileFacts:{p}   (runs the FastAPI adapter — re-parse absorbs comment edits)
        -> routeDeclList / mountDeclList / importMap:{p}   (projections)
          -> allMounts -> mountChain:{routerId}   (cross-file include_router)
          -> routeFact -> endpoint -> graph:all   (root)

Inputs (kinds with no registered query) are ``fileText:{path}`` and ``files:all``.
Cross-file router mounting is resolved at QUERY time (never eager edges), so a
change to a mount point re-resolves exactly the affected endpoints.
"""

from __future__ import annotations

from typing import Any

from .adapters.fastapi import extract, resolve_target
from .engine import Cx, Engine

ROOT = "graph:all"


def _route_id(path: str, route: dict[str, Any]) -> str:
    return f"{path}:{route['router']}:{route['method']}:{route['path']}"


def _join_prefix(parent: str, child: str) -> str:
    c = child.strip("/")
    return f"{parent.rstrip('/')}/{c}" if c else parent.rstrip("/")


def _join_path(prefix: str, literal: str) -> str:
    p = prefix.rstrip("/")
    if literal in ("", "/"):
        return f"{p}/" if p else "/"
    return f"{p}/{literal.lstrip('/')}"


def install(engine: Engine) -> None:
    def file_facts(key: str, cx: Cx) -> Any:
        path = key.split(":", 1)[1]
        return extract(cx.read(f"fileText:{path}"))

    def route_decl_list(key: str, cx: Cx) -> Any:
        return cx.read(f"fileFacts:{key.split(':', 1)[1]}")["routes"]

    def mount_decl_list(key: str, cx: Cx) -> Any:
        return cx.read(f"fileFacts:{key.split(':', 1)[1]}")["mounts"]

    def import_map(key: str, cx: Cx) -> Any:
        return cx.read(f"fileFacts:{key.split(':', 1)[1]}")["imports"]

    def all_mounts(key: str, cx: Cx) -> Any:
        out: list[dict[str, Any]] = []
        for path in cx.read("files:all"):
            imports = cx.read(f"importMap:{path}")
            for m in cx.read(f"mountDeclList:{path}"):
                target = resolve_target(m["target_expr"], imports, path)
                if target is None:
                    continue
                out.append(
                    {
                        "owner": path,
                        "router_local": m["router_local"],
                        "prefix": m["prefix"],
                        "target": target,
                        "middleware": [],  # Depends/middleware extraction is deferred to P4
                    }
                )
        return sorted(out, key=lambda m: (m["target"], m["owner"], m["router_local"], m["prefix"]))

    def mount_chain(key: str, cx: Cx) -> Any:
        router_id = key.split(":", 1)[1]  # "{file}:{router_local}"
        for m in cx.read("allMounts"):
            if m["target"] == router_id:
                parent = cx.read(f"mountChain:{m['owner']}:{m['router_local']}")
                return {
                    "prefix": _join_prefix(parent["prefix"], m["prefix"]),
                    "middleware": [*parent["middleware"], *m["middleware"]],
                }
        return {"prefix": "", "middleware": []}

    def route_fact(key: str, cx: Cx) -> Any:
        route_id = key.split(":", 1)[1]
        path = route_id.split(":", 1)[0]
        for r in cx.read(f"routeDeclList:{path}"):
            if _route_id(path, r) == route_id:
                return r
        return None

    def endpoint(key: str, cx: Cx) -> Any:
        route_id = key.split(":", 1)[1]
        parts = route_id.split(":")
        path, router = parts[0], parts[1]
        rf = cx.read(f"routeFact:{route_id}")
        if rf is None:
            return None
        chain = cx.read(f"mountChain:{path}:{router}")
        return {
            "method": rf["method"],
            "resolved_path": _join_path(chain["prefix"], rf["path"]),
            "middleware_chain": chain["middleware"],
            "handler": rf["handler"],
            "handler_file": path,
            "handler_line": rf["line"],
        }

    def graph_all(key: str, cx: Cx) -> Any:
        keys: list[str] = []
        for path in cx.read("files:all"):
            for r in cx.read(f"routeDeclList:{path}"):
                ekey = f"endpoint:{_route_id(path, r)}"
                cx.read(ekey)  # force assembly
                keys.append(ekey)
        return sorted(keys)

    engine.define_query("fileFacts", file_facts)
    engine.define_query("routeDeclList", route_decl_list)
    engine.define_query("mountDeclList", mount_decl_list)
    engine.define_query("importMap", import_map)
    engine.define_query("allMounts", all_mounts)
    engine.define_query("mountChain", mount_chain)
    engine.define_query("routeFact", route_fact)
    engine.define_query("endpoint", endpoint)
    engine.define_query("graph", graph_all)


def apply_sources(engine: Engine, sources: dict[str, str]) -> None:
    """Feed a whole project (``{repo-relative path: source text}``) as inputs."""
    engine.set_input("files:all", sorted(sources))
    for path in sorted(sources):
        engine.set_input(f"fileText:{path}", sources[path])
