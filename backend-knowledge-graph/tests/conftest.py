"""Shared fixtures: a small, realistic FastAPI-shaped hand-authored graph.

Two files (`app/main.py`, `app/routers/users.py`), a router mounted at
`/api/users`, two routes, handlers, a guard middleware, a Pydantic-style DTO
with fields, and the assembled Endpoint — enough structure that insertion order
and nested dict/list ordering genuinely exercise the canonical serializer.
"""

from __future__ import annotations

import pytest

from bkg.protocol.enums import Confidence, EdgeKind, HttpMethod
from bkg.protocol.models import (
    Auth,
    Edge,
    EndpointNode,
    FieldIR,
    FileNode,
    HandlerNode,
    MiddlewareNode,
    Param,
    PartialGraph,
    RouteNode,
    RouterMount,
    SchemaRefNode,
    SymbolRef,
)


def build_sample_graph() -> PartialGraph:
    files = (
        FileNode(id="file:app/main.py", path="app/main.py"),
        FileNode(id="file:app/routers/users.py", path="app/routers/users.py"),
    )
    routes = (
        RouteNode(
            id="route:app/routers/users.py:router:GET:/{user_id}",
            method=HttpMethod.GET,
            path="/{user_id}",
            file="app/routers/users.py",
            line=12,
            router_local="router",
        ),
        RouteNode(
            id="route:app/routers/users.py:router:POST:/",
            method=HttpMethod.POST,
            path="/",
            file="app/routers/users.py",
            line=20,
            router_local="router",
        ),
    )
    handlers = (
        HandlerNode(
            id="handler:app/routers/users.py#get_user",
            symbol="get_user",
            file="app/routers/users.py",
            line=12,
        ),
        HandlerNode(
            id="handler:app/routers/users.py#create_user",
            symbol="create_user",
            file="app/routers/users.py",
            line=20,
        ),
    )
    mw = (
        MiddlewareNode(
            id="mw:app/main.py#AuthMiddleware",
            name="AuthMiddleware",
            file="app/main.py",
            line=8,
        ),
    )
    schema = SchemaRefNode(
        id="schema:app/routers/users.py#UserCreate",
        name="UserCreate",
        fields=(
            FieldIR(
                name="email",
                type="string",
                required=True,
                format="email",
                source="validation-lib",
                confidence=Confidence.STATIC_CERTAIN,
            ),
            FieldIR(name="age", type="integer", required=False, source="static-type"),
        ),
    )
    endpoint = EndpointNode(
        id="endpoint:POST:/api/users/",
        method=HttpMethod.POST,
        resolved_path="/api/users/",
        params=(Param(name="user_id", location="path"),),
        body="schema:app/routers/users.py#UserCreate",
        auth=Auth(required=True, roles=("user",)),
        middleware_chain=("mw:app/main.py#AuthMiddleware",),
        handler_file="app/routers/users.py",
        handler_line=20,
    )
    edges = (
        Edge(
            id="edge:HANDLES:route.POST->create_user",
            kind=EdgeKind.HANDLES,
            src=routes[1].id,
            dst=handlers[1].id,
        ),
        Edge(
            id="edge:VALIDATES_WITH:create_user->UserCreate",
            kind=EdgeKind.VALIDATES_WITH,
            src=handlers[1].id,
            dst=schema.id,
        ),
        Edge(
            id="edge:GUARDED_BY:route.POST->AuthMiddleware",
            kind=EdgeKind.GUARDED_BY,
            src=routes[1].id,
            dst=mw[0].id,
            ordinal=0,
        ),
    )
    mounts = (
        RouterMount(
            mounting_file="app/main.py",
            router_local="app",
            prefix="/api/users",
            target_symbol_ref="router",
            middleware=("mw:app/main.py#AuthMiddleware",),
        ),
    )
    refs = (
        SymbolRef(
            name="router",
            from_file="app/routers/users.py",
            resolved="app/routers/users.py#router",
        ),
    )
    return PartialGraph(
        nodes=(*files, *routes, *handlers, *mw, schema, endpoint),
        edges=edges,
        symbol_refs=refs,
        router_mounts=mounts,
    )


@pytest.fixture
def sample_graph() -> PartialGraph:
    return build_sample_graph()


# --- real FastAPI source fixtures (for adapter / pipeline / service / CLI / MCP) ---
FASTAPI_MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router, prefix='/api/users')\n"
)
FASTAPI_USERS = (
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


@pytest.fixture
def fastapi_sources() -> dict[str, str]:
    return {"app/main.py": FASTAPI_MAIN, "app/routers/users.py": FASTAPI_USERS}


@pytest.fixture
def fastapi_project(tmp_path) -> str:
    (tmp_path / "app" / "routers").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(FASTAPI_MAIN, encoding="utf-8")
    (tmp_path / "app" / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "routers" / "users.py").write_text(FASTAPI_USERS, encoding="utf-8")
    return str(tmp_path)


# --- FastAPI + Pydantic (P4 depth) fixtures ---
FASTAPI_SCHEMAS = (
    "from pydantic import BaseModel\n"
    "\n"
    "class UserCreate(BaseModel):\n"
    "    email: str\n"
    "    age: int = 0\n"
    "\n"
    "class UserOut(BaseModel):\n"
    "    id: int\n"
    "    email: str\n"
)
FASTAPI_USERS_DTO = (
    "from fastapi import APIRouter, Depends\n"
    "from app.schemas import UserCreate, UserOut\n"
    "router = APIRouter()\n"
    "\n"
    "@router.get('/{user_id}', response_model=UserOut)\n"
    "def get_user(user_id: int):\n"
    "    ...\n"
    "\n"
    "@router.post('/', response_model=UserOut)\n"
    "def create_user(payload: UserCreate, token: str = Depends(auth)):\n"
    "    ...\n"
)


@pytest.fixture
def fastapi_dto_sources() -> dict[str, str]:
    return {
        "app/main.py": FASTAPI_MAIN,
        "app/routers/users.py": FASTAPI_USERS_DTO,
        "app/schemas.py": FASTAPI_SCHEMAS,
    }


@pytest.fixture
def fastapi_dto_project(tmp_path) -> str:
    (tmp_path / "app" / "routers").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(FASTAPI_MAIN, encoding="utf-8")
    (tmp_path / "app" / "schemas.py").write_text(FASTAPI_SCHEMAS, encoding="utf-8")
    (tmp_path / "app" / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "routers" / "users.py").write_text(FASTAPI_USERS_DTO, encoding="utf-8")
    return str(tmp_path)
