"""A synthetic, hand-authored FastAPI-shaped pipeline for exercising the engine
BEFORE any real parser exists (P1). It models the exact firewall structure the
real adapter will use:

    file (input) -> routeDeclList / mountDeclList (projections)
                 -> allMounts -> mountChain (cross-file router mounting)
                 -> routeFact -> endpoint -> graph:all (root)

Key firewall property: per-route facts depend on the *projection* (routeDeclList),
never on the raw file — so a comment edit that changes file bytes but not the
route list is absorbed at the projection via backdating, and nothing downstream
recomputes.

File "content" is a dict standing in for parsed source:
    {"raw_version": int,            # bumps on no-op comment edits (facts unchanged)
     "routes":  [{"router","method","path","handler","line"}],
     "mounts":  [{"router_local","prefix","target","middleware":[...]}],
     "middleware": [{"name","line"}]}

NOTE: file paths never contain ":" (POSIX, repo-relative), so keys parse cleanly.
"""

from __future__ import annotations

import copy
from typing import Any

from bkg.engine import Cx, Engine

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
    def route_decl_list(key: str, cx: Cx) -> Any:
        path = key.split(":", 1)[1]
        content = cx.read(f"file:{path}")
        return sorted(
            (
                {
                    "router": r["router"],
                    "method": r["method"],
                    "path": r["path"],
                    "handler": r["handler"],
                    "line": r["line"],
                }
                for r in content.get("routes", [])
            ),
            key=lambda r: (r["router"], r["method"], r["path"]),
        )

    def mount_decl_list(key: str, cx: Cx) -> Any:
        path = key.split(":", 1)[1]
        content = cx.read(f"file:{path}")
        return sorted(
            (
                {
                    "owner": path,
                    "router_local": m["router_local"],
                    "prefix": m["prefix"],
                    "target": m["target"],
                    "middleware": list(m.get("middleware", [])),
                }
                for m in content.get("mounts", [])
            ),
            key=lambda m: (m["router_local"], m["target"], m["prefix"]),
        )

    def all_mounts(key: str, cx: Cx) -> Any:
        out: list[dict[str, Any]] = []
        for path in cx.read("files:all"):
            out.extend(cx.read(f"mountDeclList:{path}"))
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

    engine.define_query("routeDeclList", route_decl_list)
    engine.define_query("mountDeclList", mount_decl_list)
    engine.define_query("allMounts", all_mounts)
    engine.define_query("mountChain", mount_chain)
    engine.define_query("routeFact", route_fact)
    engine.define_query("endpoint", endpoint)
    engine.define_query("graph", graph_all)


# --------------------------------------------------------------------- world
class World:
    """Mirror of the current input state, so a fresh rebuild can be constructed
    from the same content the incremental engine was fed."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.counter = 0

    def files_all(self) -> list[str]:
        return sorted(self.files)


def seed_world() -> World:
    w = World()
    w.files["app/main.py"] = {
        "raw_version": 0,
        "routes": [],
        "mounts": [
            {
                "router_local": "app",
                "prefix": "/api/users",
                "target": "app/routers/users.py:router",
                "middleware": ["AuthMiddleware"],
            }
        ],
        "middleware": [{"name": "AuthMiddleware", "line": 8}],
    }
    w.files["app/routers/users.py"] = {
        "raw_version": 0,
        "routes": [
            {"router": "router", "method": "GET", "path": "/{user_id}", "handler": "get_user", "line": 12},
            {"router": "router", "method": "POST", "path": "/", "handler": "create_user", "line": 20},
        ],
        "mounts": [],
        "middleware": [],
    }
    return w


def build_fresh(store: Any, world: World) -> Engine:
    engine = Engine(store)
    install(engine)
    apply_full(engine, world)
    return engine


def apply_full(engine: Engine, world: World) -> None:
    engine.set_input("files:all", world.files_all())
    for path in world.files_all():
        engine.set_input(f"file:{path}", world.files[path])


# --------------------------------------------------------------------- edits
def _replace(engine: Engine, world: World, path: str, content: dict[str, Any]) -> None:
    world.files[path] = content
    engine.set_input(f"file:{path}", content)


def edit_comment(engine: Engine, world: World, path: str) -> None:
    c = copy.deepcopy(world.files[path])
    c["raw_version"] += 1
    _replace(engine, world, path, c)


def edit_route_line(engine: Engine, world: World, path: str, i: int) -> None:
    c = copy.deepcopy(world.files[path])
    c["routes"][i]["line"] += 1
    _replace(engine, world, path, c)


def edit_add_route(engine: Engine, world: World, path: str) -> None:
    world.counter += 1
    name = f"r{world.counter}"
    c = copy.deepcopy(world.files[path])
    c["routes"].append(
        {"router": "router", "method": "GET", "path": f"/{name}", "handler": f"h_{name}", "line": 100}
    )
    _replace(engine, world, path, c)


def edit_del_route(engine: Engine, world: World, path: str, i: int) -> None:
    c = copy.deepcopy(world.files[path])
    del c["routes"][i]
    _replace(engine, world, path, c)


def edit_reorder_routes(engine: Engine, world: World, path: str) -> None:
    c = copy.deepcopy(world.files[path])
    c["routes"].reverse()
    _replace(engine, world, path, c)


def edit_mount_prefix(engine: Engine, world: World, path: str, i: int, prefix: str) -> None:
    c = copy.deepcopy(world.files[path])
    c["mounts"][i]["prefix"] = prefix
    _replace(engine, world, path, c)


def _empty_file(routes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"raw_version": 0, "routes": routes or [], "mounts": [], "middleware": []}


def edit_add_file(engine: Engine, world: World, path: str, content: dict[str, Any] | None = None) -> None:
    world.files[path] = copy.deepcopy(content) if content is not None else _empty_file()
    engine.set_input("files:all", world.files_all())
    engine.set_input(f"file:{path}", world.files[path])


def edit_remove_file(engine: Engine, world: World, path: str) -> None:
    del world.files[path]
    engine.set_input("files:all", world.files_all())  # drop from the manifest FIRST
    engine.remove_input(f"file:{path}")
