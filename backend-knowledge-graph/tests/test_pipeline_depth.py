"""P4 depth: DTO body/response schemas (cross-file), param classification, auth,
and the structural blast-radius — built from real FastAPI + Pydantic source."""

from __future__ import annotations

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.store import open_store

SCHEMAS = (
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

USERS = (
    "from fastapi import APIRouter, Depends\n"
    "from app.schemas import UserCreate, UserOut\n"
    "router = APIRouter()\n"
    "\n"
    "@router.get('/{user_id}', response_model=UserOut)\n"
    "def get_user(user_id: int, verbose: bool = False):\n"
    "    ...\n"
    "\n"
    "@router.post('/', response_model=UserOut)\n"
    "def create_user(payload: UserCreate, token: str = Depends(auth)):\n"
    "    ...\n"
)

MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router, prefix='/api/users')\n"
)

GET = "endpoint:app/routers/users.py:router:GET:/{user_id}"
POST = "endpoint:app/routers/users.py:router:POST:/"
USER_CREATE = "schemaRef:app/schemas.py:UserCreate"
USER_OUT = "schemaRef:app/schemas.py:UserOut"


def _sources() -> dict[str, str]:
    return {"app/main.py": MAIN, "app/routers/users.py": USERS, "app/schemas.py": SCHEMAS}


def _app(sources: dict[str, str]) -> Engine:
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, sources)
    return engine


def _changed_noninput(engine: Engine, root: str) -> set[str]:
    rows = engine.snapshot_rows(root)
    rev = engine._store.get_revision()
    return {
        r.key
        for r in rows
        if r.changed_rev == rev and not r.key.startswith("fileText:") and r.key != "files:all"
    }


def test_endpoint_carries_body_response_params_and_auth() -> None:
    engine = _app(_sources())

    post = engine.query(POST)
    assert post["body"] == "app/schemas.py:UserCreate"  # cross-file DTO resolved
    assert post["response"] == "app/schemas.py:UserOut"
    # Depends(auth): required + dependency captured; `auth` isn't a security scheme -> no scheme
    assert post["auth"] == {"required": True, "dependencies": ["auth"], "schemes": []}
    assert post["params"] == []  # payload is the body, token is a dependency

    get = engine.query(GET)
    assert get["body"] is None
    assert get["response"] == "app/schemas.py:UserOut"
    by_name = {p["name"]: p for p in get["params"]}
    assert by_name["user_id"] == {"name": "user_id", "location": "path", "type": "int", "required": True}
    assert by_name["verbose"] == {"name": "verbose", "location": "query", "type": "bool", "required": False}


def test_schema_ref_exposes_fields() -> None:
    engine = _app(_sources())
    ref = engine.query(USER_CREATE)
    assert ref["file"] == "app/schemas.py"
    assert ref["fields"] == [
        {"name": "email", "type": "str", "required": True, "default": None},
        {"name": "age", "type": "int", "required": False, "default": "0"},
    ]


def test_dto_field_edit_is_surgical_and_deterministic() -> None:
    sources = _sources()
    engine = _app(sources)
    engine.snapshot_digest(ROOT)

    edited = SCHEMAS.replace("    age: int = 0\n", "    age: int = 0\n    nickname: str | None = None\n")
    sources = {**sources, "app/schemas.py": edited}
    engine.set_input("fileText:app/schemas.py", edited)
    engine.snapshot_digest(ROOT)

    changed = _changed_noninput(engine, ROOT)
    assert USER_CREATE in changed  # the edited DTO node changed...
    assert USER_OUT not in changed  # ...the sibling DTO is firewalled (backdated)
    assert not any(k.startswith("endpoint:") for k in changed)  # endpoints reference by id -> value stable

    fresh = _app(sources)
    assert engine.snapshot_digest(ROOT) == fresh.snapshot_digest(ROOT)
    assert engine.dep_map(ROOT) == fresh.dep_map(ROOT)


def test_return_annotation_serves_as_response_model() -> None:
    schemas = "from pydantic import BaseModel\n\nclass UserOut(BaseModel):\n    id: int\n"
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def h() -> UserOut:\n"
        "    ...\n"
    )
    main = (
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router)\n"
    )
    engine = _app({"app/main.py": main, "app/routers/users.py": users, "app/schemas.py": schemas})
    ep = engine.query("endpoint:app/routers/users.py:router:GET:/x")
    assert ep["response"] == "app/schemas.py:UserOut"  # from the `-> UserOut` return annotation


def test_blast_radius_is_the_reverse_dependency_set() -> None:
    engine = _app(_sources())
    engine.query(ROOT)  # build the graph + record deps

    users_of_out = {k for k in engine.reverse_dependencies(USER_OUT) if k.startswith("endpoint:")}
    assert users_of_out == {GET, POST}  # both use UserOut as response_model

    users_of_create = {k for k in engine.reverse_dependencies(USER_CREATE) if k.startswith("endpoint:")}
    assert users_of_create == {POST}  # only POST takes a UserCreate body


_MAIN_NOPREFIX = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router)\n"
)


def test_generic_response_model_resolves() -> None:
    schemas = "from pydantic import BaseModel\n\nclass UserOut(BaseModel):\n    id: int\n"
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/', response_model=list[UserOut])\n"
        "def list_users(): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/schemas.py": schemas})
    ep = engine.query("endpoint:app/routers/users.py:router:GET:/")
    assert ep["response"] == "app/schemas.py:UserOut"  # peeled out of list[UserOut]


def test_package_dto_via_init_resolves_and_blasts() -> None:
    schemas = "from pydantic import BaseModel\n\nclass UserCreate(BaseModel):\n    email: str\n"
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import UserCreate\n"
        "router = APIRouter()\n"
        "@router.post('/')\n"
        "def create(payload: UserCreate): ...\n"
    )
    # `app.schemas` is a PACKAGE (app/schemas/__init__.py), not app/schemas.py
    engine = _app(
        {"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/schemas/__init__.py": schemas}
    )
    ep = engine.query("endpoint:app/routers/users.py:router:POST:/")
    assert ep["body"] == "app/schemas/__init__.py:UserCreate"
    engine.query(ROOT)
    closure = engine.reverse_dependencies("schemaRef:app/schemas/__init__.py:UserCreate")
    assert any(k.startswith("endpoint:") for k in closure)


def test_second_model_param_has_blast_edge() -> None:
    schemas = (
        "from pydantic import BaseModel\n\n"
        "class A(BaseModel):\n    x: int\n\n"
        "class B(BaseModel):\n    y: int\n"
    )
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import A, B\n"
        "router = APIRouter()\n"
        "@router.post('/')\n"
        "def create(a: A, b: B): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/schemas.py": schemas})
    engine.query(ROOT)
    for model in ("A", "B"):  # BOTH consumed models must have a blast edge, not just the first
        closure = engine.reverse_dependencies(f"schemaRef:app/schemas.py:{model}")
        assert any(k.startswith("endpoint:") for k in closure), f"{model} has no blast edge"


def test_inherited_fields_merged_and_base_edit_blasts_subclass() -> None:
    schemas = (
        "from pydantic import BaseModel\n\n"
        "class UserBase(BaseModel):\n    id: int\n\n"
        "class UserOut(UserBase):\n    name: str\n"
    )
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/', response_model=UserOut)\n"
        "def h(): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/schemas.py": schemas})
    ref = engine.query("schemaRef:app/schemas.py:UserOut")
    assert [f["name"] for f in ref["fields"]] == ["id", "name"]  # inherited `id` + own `name`
    engine.query(ROOT)
    # editing the BASE model must blast the subclass's schemaRef (the edge exists)
    assert "schemaRef:app/schemas.py:UserOut" in engine.reverse_dependencies(
        "schemaRef:app/schemas.py:UserBase"
    )


def test_endpoint_confidence_static_certain_vs_inferred() -> None:
    # app-root route, no derivation (no prefix, no DTO) -> static-certain
    users = "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/ping')\ndef ping(): ...\n"
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    ping = engine.query("endpoint:app/routers/users.py:router:GET:/ping")
    assert ping["confidence"] == "static-certain"
    assert ping["partial"] is False
    assert ping["source"] == "static"
    assert ping["verification_status"] == "unverified"

    # a prefixed route with a DTO body required cross-file resolution -> inferred
    post = _app(_sources()).query(POST)
    assert post["confidence"] == "inferred"
    assert post["partial"] is False


def test_partial_flag_on_unresolved_dto() -> None:
    users = (
        "from fastapi import APIRouter\n"
        "from external_lib import ExternalModel\n"
        "router = APIRouter()\n"
        "@router.post('/', response_model=ExternalModel)\n"
        "def create(payload: ExternalModel): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    ep = engine.query("endpoint:app/routers/users.py:router:POST:/")
    assert ep["partial"] is True  # model-typed refs we couldn't resolve to a project schema
    assert ep["confidence"] == "inferred"
    assert ep["body"] is None
    assert ep["response"] is None


def test_schema_ref_carries_confidence() -> None:
    ref = _app(_sources()).query(USER_CREATE)
    assert ref["confidence"] == "static-certain"
    assert ref["partial"] is False
    assert ref["source"] == "static"


def test_websocket_endpoint_is_assembled() -> None:
    users = "from fastapi import APIRouter\nrouter = APIRouter()\n@router.websocket('/ws')\ndef ws(): ...\n"
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    ep = engine.query("endpoint:app/routers/users.py:router:WEBSOCKET:/ws")
    assert ep["method"] == "WEBSOCKET"
    assert ep["resolved_path"] == "/ws"


def test_endpoint_unions_route_and_router_tags() -> None:
    users = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/a', tags=['read'])\n"
        "def a(): ...\n"
        "@router.post('/b')\n"
        "def b(): ...\n"
    )
    main = (
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/u', tags=['users'])\n"
    )
    engine = _app({"app/main.py": main, "app/routers/users.py": users})
    a = engine.query("endpoint:app/routers/users.py:router:GET:/a")
    b = engine.query("endpoint:app/routers/users.py:router:POST:/b")
    assert a["tags"] == ["read", "users"]  # route ∪ router-chain, sorted + deduped
    assert b["tags"] == ["users"]  # include_router tags apply to every route it mounts


def test_endpoint_without_tags_is_empty_list() -> None:
    users = "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/ping')\ndef ping(): ...\n"
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    assert engine.query("endpoint:app/routers/users.py:router:GET:/ping")["tags"] == []


def test_app_middleware_populates_mounted_routes_chain() -> None:
    users = "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/x')\ndef h(): ...\n"
    main = (
        "from fastapi import FastAPI\n"
        "from starlette.middleware.cors import CORSMiddleware\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.add_middleware(CORSMiddleware)\n"
        "app.add_middleware(GZipMiddleware)\n"
        "app.include_router(users.router, prefix='/u')\n"
    )
    engine = _app({"app/main.py": main, "app/routers/users.py": users})
    ep = engine.query("endpoint:app/routers/users.py:router:GET:/x")
    assert ep["middleware_chain"] == ["CORSMiddleware", "GZipMiddleware"]  # app middleware, source order


def test_app_route_gets_its_own_middleware() -> None:
    main = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "app.add_middleware(GZipMiddleware)\n"
        "@app.get('/ping')\n"
        "def ping(): ...\n"
    )
    ep = _app({"app/main.py": main}).query("endpoint:app/main.py:app:GET:/ping")
    assert ep["middleware_chain"] == ["GZipMiddleware"]
    assert ep["confidence"] == "static-certain"  # middleware excluded from certainty (like tags)


def test_no_middleware_is_empty_chain() -> None:
    users = "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/ping')\ndef ping(): ...\n"
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    assert engine.query("endpoint:app/routers/users.py:router:GET:/ping")["middleware_chain"] == []


def test_auth_scheme_inline_constructor() -> None:
    users = (
        "from fastapi import APIRouter, Depends\n"
        "from fastapi.security import HTTPBearer\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def h(cred=Depends(HTTPBearer())): ...\n"
    )
    ep = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users}).query(
        "endpoint:app/routers/users.py:router:GET:/x"
    )
    assert ep["auth"]["schemes"] == ["bearer"]


def test_auth_scheme_same_file_variable() -> None:
    users = (
        "from fastapi import APIRouter, Depends\n"
        "from fastapi.security import OAuth2PasswordBearer\n"
        "oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def h(token: str = Depends(oauth2_scheme)): ...\n"
    )
    ep = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users}).query(
        "endpoint:app/routers/users.py:router:GET:/x"
    )
    assert ep["auth"]["schemes"] == ["oauth2"]


def test_auth_scheme_resolved_cross_file() -> None:
    security = (
        "from fastapi.security import APIKeyHeader\n"
        "api_key = APIKeyHeader(name='X-Key')\n"
    )
    users = (
        "from fastapi import APIRouter, Depends\n"
        "from app.security import api_key\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def h(key: str = Depends(api_key)): ...\n"
    )
    engine = _app(
        {"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/security.py": security}
    )
    ep = engine.query("endpoint:app/routers/users.py:router:GET:/x")
    assert ep["auth"]["schemes"] == ["api-key"]  # resolved from the imported scheme var
    # editing the scheme definition blasts the endpoint (cross-file edge recorded)
    engine.query(ROOT)
    assert "endpoint:app/routers/users.py:router:GET:/x" in engine.reverse_dependencies(
        "securityMap:app/security.py"
    )


def test_no_auth_scheme_when_dependency_is_not_a_scheme() -> None:
    users = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def h(user=Depends(get_current_user)): ...\n"
    )
    ep = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users}).query(
        "endpoint:app/routers/users.py:router:GET:/x"
    )
    assert ep["auth"]["schemes"] == []  # get_current_user isn't a recognized scheme


def test_config_projection_is_incremental_and_deterministic() -> None:
    v1 = "import os\nA = os.getenv('A')\n"
    engine = _app({"app/config.py": v1})
    assert [c["name"] for c in engine.query("configDeclList:app/config.py")] == ["A"]

    v2 = "import os\nA = os.getenv('A')\nB = os.getenv('B', '1')\n"
    engine.set_input("fileText:app/config.py", v2)
    assert [c["name"] for c in engine.query("configDeclList:app/config.py")] == ["A", "B"]

    fresh = _app({"app/config.py": v2})
    assert engine.query("configDeclList:app/config.py") == fresh.query("configDeclList:app/config.py")


def test_tags_do_not_change_confidence() -> None:
    # tags are excluded from the certainty formula (like middleware) — an app-root,
    # DTO-free route stays static-certain even with tags.
    users = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/ping', tags=['health'])\n"
        "def ping(): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users})
    ep = engine.query("endpoint:app/routers/users.py:router:GET:/ping")
    assert ep["tags"] == ["health"]
    assert ep["confidence"] == "static-certain"


def test_nested_dto_edit_blasts_the_containing_endpoint() -> None:
    schemas = (
        "from pydantic import BaseModel\n\n"
        "class Address(BaseModel):\n    city: str\n\n"
        "class User(BaseModel):\n    address: Address\n"
    )
    users = (
        "from fastapi import APIRouter\n"
        "from app.schemas import User\n"
        "router = APIRouter()\n"
        "@router.post('/', response_model=User)\n"
        "def create(payload: User): ...\n"
    )
    engine = _app({"app/main.py": _MAIN_NOPREFIX, "app/routers/users.py": users, "app/schemas.py": schemas})
    engine.query(ROOT)
    # editing Address must blast the endpoint that uses User (which contains an Address)
    closure = engine.reverse_dependencies("schemaRef:app/schemas.py:Address")
    assert "schemaRef:app/schemas.py:User" in closure  # User references Address as a field
    assert any(k.startswith("endpoint:") for k in closure)  # ...and the endpoint that uses User
