"""Integration: build the backend knowledge graph from REAL FastAPI source and
prove it's correct AND incremental on actual source edits."""

from __future__ import annotations

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.store import open_store

MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router, prefix='/api/users')\n"
)

USERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "\n"
    "@router.get('/{user_id}')\n"
    "def get_user(user_id: int):\n"
    "    return user_id\n"
    "\n"
    "@router.post('/')\n"
    "def create_user():\n"
    "    return {}\n"
)


def _app(sources: dict[str, str]) -> Engine:
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, sources)
    return engine


def _fresh(sources: dict[str, str]) -> tuple[str, dict[str, list[str]]]:
    engine = _app(sources)
    return engine.snapshot_digest(ROOT), engine.dep_map(ROOT)


GET_EP = "endpoint:app/routers/users.py:router:GET:/{user_id}"
POST_EP = "endpoint:app/routers/users.py:router:POST:/"


def test_assembles_endpoints_from_real_source() -> None:
    engine = _app({"app/main.py": MAIN, "app/routers/users.py": USERS})

    get_ep = engine.query(GET_EP)
    assert get_ep["method"] == "GET"
    assert get_ep["resolved_path"] == "/api/users/{user_id}"  # cross-file mount prefix applied
    assert get_ep["handler"] == "get_user"
    assert get_ep["handler_file"] == "app/routers/users.py"

    post_ep = engine.query(POST_EP)
    assert post_ep["resolved_path"] == "/api/users/"

    assert engine.query(ROOT) == sorted([GET_EP, POST_EP])


def test_symbol_import_mount_style() -> None:
    main = (
        "from fastapi import FastAPI\n"
        "from app.routers.users import router\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/v1')\n"
    )
    engine = _app({"app/main.py": main, "app/routers/users.py": USERS})
    assert engine.query(GET_EP)["resolved_path"] == "/v1/{user_id}"


def test_comment_edit_reparses_one_file_and_cascades_to_nothing() -> None:
    """The firewall on REAL source: a comment changes the text, the adapter
    re-parses, but the extracted facts are identical -> backdate -> zero graph
    change downstream."""
    sources = {"app/main.py": MAIN, "app/routers/users.py": USERS}
    engine = _app(sources)
    engine.snapshot_digest(ROOT)  # settle

    engine.reset_counters()
    engine.set_input("fileText:app/routers/users.py", USERS + "\n# a new comment\n")
    engine.snapshot_digest(ROOT)

    # only the re-parse ran; nothing downstream recomputed
    assert engine.recomputed == {"fileFacts:app/routers/users.py"}


def test_route_edit_updates_only_its_endpoint() -> None:
    sources = {"app/main.py": MAIN, "app/routers/users.py": USERS}
    engine = _app(sources)
    engine.snapshot_digest(ROOT)

    edited = USERS.replace("/{user_id}", "/{uid}")
    sources = {"app/main.py": MAIN, "app/routers/users.py": edited}
    engine.set_input("fileText:app/routers/users.py", edited)

    new_get = "endpoint:app/routers/users.py:router:GET:/{uid}"
    assert engine.query(new_get)["resolved_path"] == "/api/users/{uid}"
    assert engine.query(POST_EP)["resolved_path"] == "/api/users/"  # sibling intact
    assert engine.snapshot_digest(ROOT) == _fresh(sources)[0]


def test_mount_prefix_edit_reresolves_all_endpoints() -> None:
    sources = {"app/main.py": MAIN, "app/routers/users.py": USERS}
    engine = _app(sources)
    engine.snapshot_digest(ROOT)

    edited_main = MAIN.replace("/api/users", "/v2")
    sources = {"app/main.py": edited_main, "app/routers/users.py": USERS}
    engine.set_input("fileText:app/main.py", edited_main)

    assert engine.query(GET_EP)["resolved_path"] == "/v2/{user_id}"
    assert engine.query(POST_EP)["resolved_path"] == "/v2/"
    fresh_digest, fresh_deps = _fresh(sources)
    assert engine.snapshot_digest(ROOT) == fresh_digest
    assert engine.dep_map(ROOT) == fresh_deps


def test_incremental_matches_rebuild_across_source_edits() -> None:
    sources = {"app/main.py": MAIN, "app/routers/users.py": USERS}
    engine = _app(sources)
    engine.snapshot_digest(ROOT)

    edits = [
        ("app/routers/users.py", USERS + "\n@router.delete('/{user_id}')\ndef delete_user(user_id): ...\n"),
        ("app/main.py", MAIN.replace("/api/users", "/api/v1/users")),
        ("app/routers/users.py", USERS),  # revert the added route
    ]
    for path, text in edits:
        sources[path] = text
        engine.set_input(f"fileText:{path}", text)
        assert engine.snapshot_digest(ROOT) == _fresh(sources)[0]
        assert engine.dep_map(ROOT) == _fresh(sources)[1]
