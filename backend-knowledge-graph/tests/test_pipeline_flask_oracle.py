"""Determinism oracle for the Flask + multi-framework path (the FastAPI oracle
never exercised the registry/Flask code). Random edits to a generated Flask
project must keep the incrementally-maintained graph byte-identical to a rebuild,
with dependency-edge equality and idempotence — and a mixed FastAPI+Flask project
must be deterministic too.
"""

from __future__ import annotations

import random

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.store import open_store


def _app(sources: dict[str, str]) -> Engine:
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, sources)
    return engine


class FlaskWorld:
    def __init__(self) -> None:
        self.routes: list[dict[str, str]] = [
            {"method": "GET", "path": "/<int:user_id>", "handler": "get_user"},
            {"method": "POST", "path": "/", "handler": "create_user"},
        ]
        self.prefix = "/api/users"
        self.n = 0

    def _main(self) -> str:
        return (
            "from flask import Flask\n"
            "from app.users import users_bp\n"
            "app = Flask(__name__)\n"
            f"app.register_blueprint(users_bp, url_prefix='{self.prefix}')\n"
        )

    def _users(self) -> str:
        lines = ["from flask import Blueprint", "users_bp = Blueprint('users', __name__)", ""]
        for r in self.routes:
            if r["method"] == "GET":
                lines.append(f"@users_bp.route('{r['path']}')")
            else:
                lines.append(f"@users_bp.route('{r['path']}', methods=['{r['method']}'])")
            lines.append(f"def {r['handler']}(): ...")
            lines.append("")
        return "\n".join(lines) + "\n"

    def sources(self) -> dict[str, str]:
        return {"app/main.py": self._main(), "app/users.py": self._users()}


def _edit(rng: random.Random, w: FlaskWorld) -> None:
    ops = ["add_route", "prefix", "converter"]
    if len(w.routes) > 1:
        ops.append("remove_route")
    op = rng.choice(ops)
    if op == "add_route":
        w.n += 1
        path = rng.choice([f"/r{w.n}", f"/r{w.n}/<int:id{w.n}>", f"/files{w.n}/<path:p{w.n}>"])
        w.routes.append({"method": "GET", "path": path, "handler": f"h{w.n}"})
    elif op == "remove_route":
        del w.routes[rng.randrange(len(w.routes))]
    elif op == "prefix":
        w.prefix = rng.choice(["/api/users", "/v2", "/u", ""])
    elif op == "converter":
        w.n += 1
        rng.choice(w.routes)["path"] = rng.choice([f"/<int:x{w.n}>", f"/<path:x{w.n}>", f"/item{w.n}"])


def test_flask_determinism_fuzz() -> None:
    for seed in range(12):
        rng = random.Random(seed)
        world = FlaskWorld()
        engine = _app(world.sources())
        engine.snapshot_digest(ROOT)
        for step in range(12):
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


# --- a MIXED FastAPI + Flask + pure-DTO project (exercises per-file detection) ---
_MIXED = {
    "app/fastapi_main.py": (
        "from fastapi import FastAPI\n"
        "from app.fastapi_users import router\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/fa')\n"
    ),
    "app/fastapi_users.py": (
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/{uid}', response_model=UserOut)\n"
        "def get_user(uid: int): ...\n"
    ),
    "app/flask_main.py": (
        "from flask import Flask\n"
        "from app.flask_users import bp\n"
        "app = Flask(__name__)\n"
        "app.register_blueprint(bp, url_prefix='/fl')\n"
    ),
    "app/flask_users.py": (
        "from flask import Blueprint\n"
        "bp = Blueprint('u', __name__)\n"
        "@bp.route('/<int:uid>')\n"
        "def show(uid): ...\n"
    ),
    "app/schemas.py": "from pydantic import BaseModel\n\nclass UserOut(BaseModel):\n    id: int\n",
}


def test_mixed_framework_project_builds_and_stays_deterministic() -> None:
    engine = _app(_MIXED)
    fa = engine.query("endpoint:app/fastapi_users.py:router:GET:/{uid}")
    fl = engine.query("endpoint:app/flask_users.py:bp:GET:/{uid}")
    assert fa["resolved_path"] == "/fa/{uid}"
    assert fl["resolved_path"] == "/fl/{uid}"

    engine.snapshot_digest(ROOT)
    edited = dict(_MIXED)
    edited["app/flask_main.py"] = _MIXED["app/flask_main.py"].replace("/fl", "/flask")
    engine.set_input("fileText:app/flask_main.py", edited["app/flask_main.py"])
    assert engine.query("endpoint:app/flask_users.py:bp:GET:/{uid}")["resolved_path"] == "/flask/{uid}"
    fresh = _app(edited)
    assert engine.snapshot_digest(ROOT) == fresh.snapshot_digest(ROOT)
    assert engine.dep_map(ROOT) == fresh.dep_map(ROOT)


def test_path_converter_survives_extraction() -> None:
    # a Flask <path:name> route -> resolved_path keeps the :path converter
    src = {
        "app/main.py": (
            "from flask import Flask\nfrom app.f import bp\n"
            "app=Flask(__name__)\napp.register_blueprint(bp)\n"
        ),
        "app/f.py": (
            "from flask import Blueprint\nbp=Blueprint('f', __name__)\n"
            "@bp.route('/files/<path:name>')\ndef d(name): ...\n"
        ),
    }
    engine = _app(src)
    ep = engine.query("endpoint:app/f.py:bp:GET:/files/{name:path}")
    assert ep["resolved_path"] == "/files/{name:path}"
