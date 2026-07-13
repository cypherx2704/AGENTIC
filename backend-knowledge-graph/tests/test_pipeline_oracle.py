"""Real-pipeline determinism oracle — fuzzes the FULL P4 depth layer (extract over
real text, cross-file DTO/mount resolution, schemaRef edges, inherited fields).

A `DtoWorld` deterministically generates a 3-file FastAPI+Pydantic project; random
edits mutate DTOs / routes / mounts and regenerate the source. After each edit the
incrementally-maintained graph must be byte-identical and dependency-edge-identical
to a from-scratch rebuild, and a redundant re-query must recompute nothing.
"""

from __future__ import annotations

import random
import re
from typing import Any

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.store import open_store


class DtoWorld:
    def __init__(self) -> None:
        self.models: dict[str, list[str]] = {
            "UserBase": ["id:int"],
            "UserCreate": ["email:str"],
            "UserOut": ["name:str"],
        }
        self.bases: dict[str, str] = {"UserCreate": "UserBase", "UserOut": "UserBase"}
        self.routes: list[dict[str, Any]] = [
            {
                "method": "get",
                "path": "/{user_id}",
                "handler": "get_user",
                "body": None,
                "response": "UserOut",
            },
            {
                "method": "post",
                "path": "/",
                "handler": "create_user",
                "body": "UserCreate",
                "response": "UserOut",
            },
        ]
        self.prefix = "/api/users"
        self.router_tags: list[str] = []
        self.middlewares: list[str] = []
        self.n = 0

    def _schemas(self) -> str:
        lines = ["from pydantic import BaseModel", ""]
        for name in sorted(self.models):
            lines.append(f"class {name}({self.bases.get(name, 'BaseModel')}):")
            fields = self.models[name]
            if not fields:
                lines.append("    pass")
            for f in fields:
                n, t = f.split(":")
                lines.append(f"    {n}: {t}")
            lines.append("")
        return "\n".join(lines) + "\n"

    def _users(self) -> str:
        used = sorted(
            {r["body"] for r in self.routes if r["body"]}
            | {r["response"] for r in self.routes if r["response"]}
        )
        lines = ["from fastapi import APIRouter, Depends"]
        if used:
            lines.append(f"from app.schemas import {', '.join(used)}")
        lines += ["router = APIRouter()", ""]
        for r in self.routes:
            rm = f", response_model={r['response']}" if r["response"] else ""
            tg = f", tags={r['tags']}" if r.get("tags") else ""
            lines.append(f"@router.{r['method']}('{r['path']}'{rm}{tg})")
            params = [f"{pp}: int" for pp in re.findall(r"\{([^}:]+)", str(r["path"]))]
            if r["body"]:
                params.append(f"payload: {r['body']}")
            params.append("token: str = Depends(auth)")
            lines.append(f"def {r['handler']}({', '.join(params)}): ...")
            lines.append("")
        return "\n".join(lines) + "\n"

    def _main(self) -> str:
        rt = f", tags={self.router_tags}" if self.router_tags else ""
        mw = "".join(f"app.add_middleware({m})\n" for m in self.middlewares)
        return (
            "from fastapi import FastAPI\n"
            "from app.routers import users\n"
            "app = FastAPI()\n"
            f"{mw}"
            f"app.include_router(users.router, prefix='{self.prefix}'{rt})\n"
        )

    def sources(self) -> dict[str, str]:
        return {
            "app/main.py": self._main(),
            "app/routers/users.py": self._users(),
            "app/schemas.py": self._schemas(),
        }


def _edit(rng: random.Random, w: DtoWorld) -> None:
    ops = [
        "add_field", "rename_field", "remove_field", "add_route",
        "swap_response", "prefix", "route_tags", "router_tags", "middleware",
    ]
    if len(w.routes) > 1:
        ops.append("remove_route")
    if any(r["body"] for r in w.routes):
        ops.append("swap_body")
    op = rng.choice(ops)
    if op == "add_field":
        w.n += 1
        m = rng.choice(list(w.models))
        w.models[m] = w.models[m] + [f"f{w.n}:int"]
    elif op == "rename_field":
        m = rng.choice([k for k in w.models if w.models[k]] or list(w.models))
        if w.models[m]:
            w.n += 1
            i = rng.randrange(len(w.models[m]))
            n, t = w.models[m][i].split(":")
            w.models[m][i] = f"{n}{w.n}:{t}"
    elif op == "remove_field":
        m = rng.choice([k for k in w.models if w.models[k]] or list(w.models))
        if w.models[m]:
            del w.models[m][rng.randrange(len(w.models[m]))]
    elif op == "add_route":
        w.n += 1
        w.routes.append(
            {
                "method": "get",
                "path": f"/r{w.n}",
                "handler": f"h{w.n}",
                "body": None,
                "response": rng.choice(["UserOut", None]),
            }
        )
    elif op == "remove_route":
        del w.routes[rng.randrange(len(w.routes))]
    elif op == "swap_response":
        r = rng.choice(w.routes)
        r["response"] = rng.choice(["UserOut", "UserCreate", None])
    elif op == "swap_body":
        r = rng.choice([r for r in w.routes if r["body"]])
        r["body"] = rng.choice(["UserCreate", "UserOut"])
    elif op == "prefix":
        w.prefix = rng.choice(["/api/users", "/v2", "/u", ""])
    elif op == "route_tags":
        # includes reordered + empty variants: the adapter sorts+dedupes, so every
        # form must hash identical to a rebuild (the tag-path determinism proof).
        r = rng.choice(w.routes)
        r["tags"] = rng.choice([["a"], ["a", "b"], ["b", "a"], ["b", "a", "b"], []])
    elif op == "router_tags":
        w.router_tags = rng.choice([["users"], ["y", "x"], []])
    elif op == "middleware":
        # source-ordered (NOT sorted): ["B","A"] differs from ["A","B"], but each state
        # is deterministic, so incremental must still match the rebuild.
        w.middlewares = rng.choice([[], ["A"], ["A", "B"], ["B", "A"]])


def _app(sources: dict[str, str]) -> Engine:
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, sources)
    return engine


def test_real_pipeline_determinism_fuzz() -> None:
    for seed in range(15):
        rng = random.Random(seed)
        world = DtoWorld()
        engine = _app(world.sources())
        engine.snapshot_digest(ROOT)
        for step in range(15):
            _edit(rng, world)
            for path, text in world.sources().items():
                engine.set_input(f"fileText:{path}", text)

            inc_digest = engine.snapshot_digest(ROOT)
            inc_deps = engine.dep_map(ROOT)

            engine.reset_counters()
            engine.snapshot_digest(ROOT)
            assert engine.recompute_count == 0, (
                f"seed={seed} step={step}: non-incremental {engine.recomputed}"
            )

            fresh = _app(world.sources())
            assert inc_digest == fresh.snapshot_digest(ROOT), f"seed={seed} step={step}: digest mismatch"
            assert inc_deps == fresh.dep_map(ROOT), f"seed={seed} step={step}: dep-edge mismatch"
