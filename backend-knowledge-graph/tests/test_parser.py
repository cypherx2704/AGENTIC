"""Unit tests for the parser boundary: ``bkg.parser.analyze(source, path) -> PartialGraph``.

Asserts the file-local nodes + router mounts + the pre-computed cross-file resolution
CANDIDATE ids the (language-neutral) engine stitches on. Determinism is checked directly
(``analyze`` is pure) and end-to-end over the shared corpus.
"""

from __future__ import annotations

import pytest

from bkg.parser import analyze
from bkg.protocol.canonical import canonical_bytes
from bkg.service import GraphService
from parity_corpus import CORPUS, PROJECTS


def _nodes(source: str, path: str, kind: str) -> list[dict]:
    return [n for n in analyze(source, path).to_dict()["nodes"] if n["kind"] == kind]


def test_fastapi_route_carries_resolved_candidates() -> None:
    src = (
        "from fastapi import APIRouter, Depends\n"
        "from app.schemas import UserCreate, UserOut\n"
        "from app.security import oauth2\n"
        "router = APIRouter()\n"
        "@router.post('/', response_model=UserOut, tags=['w'])\n"
        "def create_user(payload: UserCreate, token: str = Depends(oauth2)): ...\n"
    )
    (route,) = _nodes(src, "app/routers/users.py", "Route")
    assert route["method"] == "POST" and route["path"] == "/" and route["tags"] == ["w"]
    assert "app/schemas.py:UserOut" in route["response_candidates"]
    params = {p["name"]: p for p in route["params"]}
    assert "app/schemas.py:UserCreate" in params["payload"]["dto_candidates"]
    assert params["token"]["depends"] == "oauth2"
    assert "app/security.py:oauth2" in params["token"]["scheme_candidates"]


def test_schema_node_carries_bases_and_field_refs() -> None:
    src = (
        "from pydantic import BaseModel\n"
        "from app.schemas import Address\n"
        "class UserOut(BaseModel):\n    id: int\n    home: Address\n"
    )
    (schema,) = _nodes(src, "app/models.py", "SchemaRef")
    assert schema["name"] == "UserOut" and schema["bases"] == ["BaseModel"]
    fields = {f["name"]: f for f in schema["fields"]}
    assert fields["id"]["ref_candidates"] == []  # scalar
    assert "app/schemas.py:Address" in fields["home"]["ref_candidates"]  # nested DTO


def test_inline_security_scheme_classified() -> None:
    src = (
        "from fastapi import APIRouter, Depends\n"
        "from fastapi.security import HTTPBearer\n"
        "router = APIRouter()\n"
        "@router.get('/x')\n"
        "def x(t: str = Depends(HTTPBearer())): ...\n"
    )
    (route,) = _nodes(src, "app/x.py", "Route")
    (param,) = [p for p in route["params"] if p["depends"]]
    assert param["scheme_inline"] == "bearer"


def test_config_and_security_nodes() -> None:
    src = (
        "import os\n"
        "from fastapi.security import OAuth2PasswordBearer\n"
        "from pydantic_settings import BaseSettings\n"
        "oauth2 = OAuth2PasswordBearer(tokenUrl='t')\n"
        "A = os.getenv('A', 'x')\n"
        "class Settings(BaseSettings):\n    debug: bool = False\n"
    )
    configs = {c["name"]: c for c in _nodes(src, "app/conf.py", "Config")}
    assert configs["A"]["config_kind"] == "env" and configs["A"]["default"] == "'x'"
    assert configs["debug"]["config_kind"] == "setting" and configs["debug"]["cls"] == "Settings"
    (sec,) = _nodes(src, "app/conf.py", "SecurityScheme")
    assert sec["var"] == "oauth2" and sec["scheme"] == "oauth2"


def test_flask_mount_and_path_normalization() -> None:
    src = (
        "from flask import Blueprint\n"
        "bp = Blueprint('bp', __name__)\n"
        "@bp.route('/u/<int:user_id>', methods=['GET', 'DELETE'])\n"
        "def u(user_id): ...\n"
    )
    routes = _nodes(src, "app/views.py", "Route")
    assert {r["method"] for r in routes} == {"GET", "DELETE"}
    assert all(r["path"] == "/u/{user_id}" for r in routes)  # <int:x> normalized


def test_syntax_error_is_partial() -> None:
    pg = analyze("def broken(:\n    ...\n", "app/bad.py")
    d = pg.to_dict()
    assert d["partial"] is True and d["nodes"] == []


@pytest.mark.parametrize("name,src", CORPUS, ids=[n for n, _ in CORPUS])
def test_analyze_is_deterministic(name: str, src: str) -> None:
    assert canonical_bytes(analyze(src, "m.py").to_dict()) == canonical_bytes(analyze(src, "m.py").to_dict())


@pytest.mark.parametrize("name,sources", PROJECTS, ids=[n for n, _ in PROJECTS])
def test_project_digest_is_deterministic(name: str, sources: dict[str, str]) -> None:
    assert GraphService.from_sources(sources).snapshot_digest() == (
        GraphService.from_sources(sources).snapshot_digest()
    )
