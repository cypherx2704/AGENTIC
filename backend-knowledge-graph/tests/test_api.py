"""The Backend Intelligence HTTP API — a thin transport over GraphService.

Skips entirely without the optional ``bkg[api]`` extra (fastapi + httpx)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from bkg.api import build_app  # noqa: E402
from bkg.service import GraphService  # noqa: E402

TAGGED_USERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.get('/{user_id}', tags=['users'])\n"
    "def get_user(user_id: int): ...\n"
    "@router.post('/', tags=['admin'])\n"
    "def create_user(): ...\n"
)
TAGGED_MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router, prefix='/api/users')\n"
)

DTO_SCHEMAS = "from pydantic import BaseModel\n\nclass UserCreate(BaseModel):\n    email: str\n"
DTO_USERS = (
    "from fastapi import APIRouter\n"
    "from app.schemas import UserCreate\n"
    "router = APIRouter()\n"
    "@router.post('/', response_model=UserCreate)\n"
    "def create(payload: UserCreate): ...\n"
)
DTO_MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router)\n"
)


def _service() -> GraphService:
    return GraphService.from_sources({"app/main.py": TAGGED_MAIN, "app/routers/users.py": TAGGED_USERS})


def _dto_service() -> GraphService:
    return GraphService.from_sources(
        {"app/main.py": DTO_MAIN, "app/routers/users.py": DTO_USERS, "app/schemas.py": DTO_SCHEMAS}
    )


def test_list_endpoints_passthrough() -> None:
    body = TestClient(build_app(_service())).get("/graph/endpoints").json()
    assert body["count"] == 2
    assert {e["resolved_path"] for e in body["data"]} == {"/api/users/{user_id}", "/api/users/"}
    assert all(e["repo"] == "default" for e in body["data"])  # composite (repo, id) addressing


def test_filter_and_search_query_params() -> None:
    client = TestClient(build_app(_service()))
    assert {e["method"] for e in client.get("/graph/endpoints?method=GET").json()["data"]} == {"GET"}
    assert len(client.get("/graph/endpoints?tag=users").json()["data"]) == 1
    assert len(client.get("/graph/search?q=admin").json()["data"]) == 1  # matches a tag
    assert len(client.get("/graph/search?q=").json()["data"]) == 2  # empty query = all


def test_get_endpoint_by_method_and_path() -> None:
    svc = GraphService.from_sources(
        {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ping')\ndef ping(): ...\n"}
    )
    client = TestClient(build_app(svc))
    r = client.get("/graph/endpoints/GET/ping")
    assert r.status_code == 200
    assert r.json()["data"]["handler"] == "ping"
    assert client.get("/graph/endpoints/GET/nope").status_code == 404


def test_get_endpoint_by_id() -> None:
    svc = _service()
    client = TestClient(build_app(svc))
    some_id = svc.list_endpoints()[0]["id"]
    r = client.get("/graph/endpoints/by-id", params={"id": some_id})
    assert r.status_code == 200 and r.json()["data"]["id"] == some_id
    assert client.get("/graph/endpoints/by-id", params={"id": "nope"}).status_code == 404


def test_schemas_and_blast_radius() -> None:
    client = TestClient(build_app(_dto_service()))
    schemas = client.get("/graph/schemas").json()["data"]
    assert any(s["id"] == "app/schemas.py:UserCreate" for s in schemas)
    blast = client.get("/graph/blast-radius/app/schemas.py:UserCreate").json()["data"]
    assert "POST:/" in blast  # DTO_MAIN mounts without a prefix -> resolved-route id
    empty = client.get("/graph/blast-radius/app/schemas.py:Missing")  # unreferenced -> 200 empty
    assert empty.status_code == 200 and empty.json()["data"] == []


def test_trust_summary() -> None:
    data = TestClient(build_app(_dto_service())).get("/graph/trust").json()["data"]
    assert data["endpoints"] == 1


def test_config_endpoint() -> None:
    svc = GraphService.from_sources(
        {"app/config.py": "import os\nDB = os.getenv('DATABASE_URL', 'sqlite://')\n"}
    )
    data = TestClient(build_app(svc)).get("/graph/config").json()["data"]
    assert any(c["name"] == "DATABASE_URL" and c["repo"] == "default" for c in data)


def test_etag_enables_304() -> None:
    client = TestClient(build_app(_service()))
    etag = client.get("/graph/endpoints").headers["etag"]
    assert etag
    assert client.get("/graph/endpoints", headers={"If-None-Match": etag}).status_code == 304


def test_etag_is_per_route_and_per_query() -> None:
    # the ETag must key on request identity, not just the whole-graph digest —
    # else a stale If-None-Match from one route/query would 304 a different response
    client = TestClient(build_app(_service()))
    endpoints_etag = client.get("/graph/endpoints").headers["etag"]
    # cross-route: schemas must NOT 304 on the endpoints ETag
    assert client.get("/graph/schemas", headers={"If-None-Match": endpoints_etag}).status_code == 200
    # cross-query: different filters -> different ETags
    get_etag = client.get("/graph/endpoints?method=GET").headers["etag"]
    post_etag = client.get("/graph/endpoints?method=POST").headers["etag"]
    assert get_etag != post_etag


def test_repo_scoped_routes_and_default_alias() -> None:
    client = TestClient(build_app(_service(), repo_id="myrepo"))
    assert client.get("/repos/myrepo/graph/endpoints").json()["count"] == 2
    assert client.get("/repos/wrong/graph/endpoints").status_code == 404  # unknown repo
    assert client.get("/graph/endpoints").json()["repo"] == "myrepo"  # default alias -> configured repo


def test_read_only_surface_and_reserved_runner() -> None:
    client = TestClient(build_app(_service()))
    assert client.post("/graph/endpoints").status_code == 405  # GET-only route
    assert client.post("/runner/execute").status_code == 404  # reserved, not built (Phase 6)


def test_healthz() -> None:
    body = TestClient(build_app(_service())).get("/healthz").json()
    assert body["status"] == "ok"
    assert "app/main.py" in body["files"]


def test_cors_allows_localhost_only() -> None:
    client = TestClient(build_app(_service()))
    ok = client.get("/graph/endpoints", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    evil = client.get("/graph/endpoints", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_refresh_before_serving_reflects_disk_edits(tmp_path) -> None:
    from bkg.daemon import Daemon

    (tmp_path / "app" / "routers").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(TAGGED_MAIN, encoding="utf-8")
    users = tmp_path / "app" / "routers" / "users.py"
    users.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/a')\ndef a(): ...\n",
        encoding="utf-8",
    )
    daemon = Daemon(str(tmp_path), db_path=":memory:")
    client = TestClient(build_app(daemon.service, refresh=daemon.resync))
    assert client.get("/graph/endpoints").json()["count"] == 1

    # add a route on disk; the API resyncs before serving -> sees it with no restart
    users.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/a')\ndef a(): ...\n@router.get('/b')\ndef b(): ...\n",
        encoding="utf-8",
    )
    body = client.get("/graph/endpoints").json()
    assert body["count"] == 2
    # freshness == the oracle at the HTTP layer: incremental digest == a rebuild's
    assert body["digest"] == GraphService.from_directory(str(tmp_path)).snapshot_digest()
