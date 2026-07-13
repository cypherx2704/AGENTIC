"""Flask blueprints resolve through the SAME cross-file machinery as FastAPI
routers — proving the adapter contract, not the pipeline, is framework-specific."""

from __future__ import annotations

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.store import open_store

MAIN = (
    "from flask import Flask\n"
    "from app.users import users_bp\n"
    "app = Flask(__name__)\n"
    "app.register_blueprint(users_bp, url_prefix='/api/users')\n"
)
USERS = (
    "from flask import Blueprint\n"
    "users_bp = Blueprint('users', __name__)\n"
    "@users_bp.route('/<int:user_id>')\n"
    "def get_user(user_id): ...\n"
    "@users_bp.route('/', methods=['POST'])\n"
    "def create_user(): ...\n"
)

GET = "endpoint:app/users.py:users_bp:GET:/{user_id}#0"
POST = "endpoint:app/users.py:users_bp:POST:/#0"


def _app(sources: dict[str, str]) -> Engine:
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, sources)
    return engine


def test_blueprint_prefix_resolves_cross_file() -> None:
    engine = _app({"app/main.py": MAIN, "app/users.py": USERS})
    assert engine.query(GET)["resolved_path"] == "/api/users/{user_id}"
    assert engine.query(POST)["resolved_path"] == "/api/users/"
    assert engine.query(GET)["handler"] == "get_user"


def test_flask_endpoints_have_empty_tags_and_middleware() -> None:
    # Flask carries no tags/middleware — the pipeline's .get defaults make it uniform
    engine = _app({"app/main.py": MAIN, "app/users.py": USERS})
    assert engine.query(GET)["tags"] == []
    assert engine.query(GET)["middleware_chain"] == []
    assert engine.query(POST)["tags"] == []


def test_blueprint_prefix_edit_is_incremental_and_deterministic() -> None:
    sources = {"app/main.py": MAIN, "app/users.py": USERS}
    engine = _app(sources)
    engine.snapshot_digest(ROOT)

    edited_main = MAIN.replace("/api/users", "/api/v2")
    sources = {"app/main.py": edited_main, "app/users.py": USERS}
    engine.set_input("fileText:app/main.py", edited_main)

    assert engine.query(GET)["resolved_path"] == "/api/v2/{user_id}"
    fresh = _app(sources)
    assert engine.snapshot_digest(ROOT) == fresh.snapshot_digest(ROOT)
    assert engine.dep_map(ROOT) == fresh.dep_map(ROOT)
