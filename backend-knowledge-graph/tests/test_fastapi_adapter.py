"""Unit tests for the FastAPI adapter's ast-based extraction + import resolution."""

from __future__ import annotations

from bkg.adapters.fastapi import extract, resolve_target
from bkg.protocol.canonical import canonical_bytes


def test_extract_output_is_deterministic_and_canonical() -> None:
    """Adapter conformance: extraction is a pure, deterministic function whose
    output is strictly canonical-serializable (no floats/tuples/etc. leak in)."""
    src = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/a')\n"
        "def a(): ...\n"
        "@router.post('/b')\n"
        "def b(): ...\n"
    )
    assert canonical_bytes(extract(src)) == canonical_bytes(extract(src))


def test_extracts_routes() -> None:
    src = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/{user_id}')\n"
        "def get_user(user_id: int):\n"
        "    return user_id\n"
        "\n"
        "@router.post('/')\n"
        "async def create_user():\n"
        "    return {}\n"
    )
    facts = extract(src)
    routes = facts["routes"]
    assert len(routes) == 2
    # line is the function `def` line (line 5), not the decorator line (line 4)
    assert routes[0] == {
        "router": "router",
        "method": "GET",
        "path": "/{user_id}",
        "handler": "get_user",
        "line": 5,
    }
    assert routes[1]["method"] == "POST"
    assert routes[1]["handler"] == "create_user"


def test_ignores_non_route_decorators_and_non_literal_paths() -> None:
    src = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "P = '/x'\n"
        "@some.decorator('/y')\n"  # not an HTTP method
        "def a(): ...\n"
        "@router.get(P)\n"  # non-literal path -> deferred
        "def b(): ...\n"
    )
    assert extract(src)["routes"] == []


def test_extracts_include_router_mounts() -> None:
    src = (
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/api/users')\n"
    )
    mounts = extract(src)["mounts"]
    assert len(mounts) == 1
    assert mounts[0] == {"router_local": "app", "prefix": "/api/users", "target_expr": "users.router"}


def test_syntax_error_is_partial_not_crash() -> None:
    facts = extract("def broken(:\n")
    assert facts["partial"] is True
    assert facts["routes"] == []


def test_resolve_absolute_module_attribute() -> None:
    # from app.routers import users ; include_router(users.router)
    imports = {"users": {"module": "app.routers", "name": "users", "level": 0}}
    assert resolve_target("users.router", imports, "app/main.py") == "app/routers/users.py:router"


def test_resolve_absolute_symbol_import() -> None:
    # from app.routers.users import router ; include_router(router)
    imports = {"router": {"module": "app.routers.users", "name": "router", "level": 0}}
    assert resolve_target("router", imports, "app/main.py") == "app/routers/users.py:router"


def test_resolve_relative_import() -> None:
    # from .routers import users  (in app/main.py)
    imports = {"users": {"module": "routers", "name": "users", "level": 1}}
    assert resolve_target("users.router", imports, "app/main.py") == "app/routers/users.py:router"


def test_resolve_local_router() -> None:
    # router defined in the same file, not imported
    assert resolve_target("router", {}, "app/main.py") == "app/main.py:router"


def test_extract_tolerates_leading_bom() -> None:
    src = "﻿" + "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/x')\ndef h(): ...\n"
    assert len(extract(src)["routes"]) == 1
