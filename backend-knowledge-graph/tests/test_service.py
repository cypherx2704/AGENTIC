"""GraphService: endpoint listing/lookup, incremental change, and directory load."""

from __future__ import annotations

from bkg.service import GraphService, load_directory


def test_list_endpoints(fastapi_sources: dict[str, str]) -> None:
    endpoints = GraphService.from_sources(fastapi_sources).list_endpoints()
    pairs = {(e["method"], e["resolved_path"]) for e in endpoints}
    assert ("GET", "/api/users/{user_id}") in pairs
    assert ("POST", "/api/users/") in pairs
    assert all("id" in e and "handler" in e and "handler_file" in e for e in endpoints)


def test_get_endpoint(fastapi_sources: dict[str, str]) -> None:
    service = GraphService.from_sources(fastapi_sources)
    ep = service.get_endpoint("get", "/api/users/{user_id}")  # case-insensitive method
    assert ep is not None
    assert ep["handler"] == "get_user"
    assert service.get_endpoint("GET", "/does/not/exist") is None


def test_apply_change_is_incremental_and_correct(fastapi_sources: dict[str, str]) -> None:
    service = GraphService.from_sources(fastapi_sources)
    before = service.list_endpoints()

    service.apply_change("app/routers/users.py", fastapi_sources["app/routers/users.py"] + "\n# comment\n")
    assert service.list_endpoints() == before  # a comment changes no endpoints (only re-parses)

    service.apply_change("app/main.py", fastapi_sources["app/main.py"].replace("/api/users", "/v2"))
    assert service.get_endpoint("GET", "/v2/{user_id}") is not None
    assert service.get_endpoint("GET", "/api/users/{user_id}") is None


def test_from_directory(fastapi_project: str) -> None:
    sources = load_directory(fastapi_project)
    assert "app/main.py" in sources
    assert "app/routers/users.py" in sources

    service = GraphService.from_directory(fastapi_project)
    assert service.get_endpoint("GET", "/api/users/{user_id}") is not None


def test_list_schemas(fastapi_dto_sources: dict[str, str]) -> None:
    ids = {s["id"] for s in GraphService.from_sources(fastapi_dto_sources).list_schemas()}
    assert {"app/schemas.py:UserCreate", "app/schemas.py:UserOut"} <= ids


def test_blast_radius(fastapi_dto_sources: dict[str, str]) -> None:
    service = GraphService.from_sources(fastapi_dto_sources)
    assert service.blast_radius("app/schemas.py:UserCreate") == ["app/routers/users.py:router:POST:/"]
    affected = set(service.blast_radius("app/schemas.py:UserOut"))
    assert affected == {"app/routers/users.py:router:GET:/{user_id}", "app/routers/users.py:router:POST:/"}
    assert service.blast_radius("app/schemas.py:DoesNotExist") == []


def test_trust_summary(fastapi_dto_sources: dict[str, str]) -> None:
    summary = GraphService.from_sources(fastapi_dto_sources).trust_summary()
    assert summary["endpoints"] == 2
    assert summary["partial"] == 0
    assert sum(summary["by_confidence"].values()) == 2


_TAGGED_USERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.get('/{user_id}', tags=['users'])\n"
    "def get_user(user_id: int): ...\n"
    "@router.post('/', tags=['users', 'admin'])\n"
    "def create_user(): ...\n"
)
_TAGGED_MAIN = (
    "from fastapi import FastAPI\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.include_router(users.router, prefix='/api/users')\n"
)


def _tagged_service() -> GraphService:
    return GraphService.from_sources({"app/main.py": _TAGGED_MAIN, "app/routers/users.py": _TAGGED_USERS})


def test_filter_by_method_is_case_insensitive_exact() -> None:
    service = _tagged_service()
    assert {e["method"] for e in service.filter_by_method("get")} == {"GET"}
    assert [e["method"] for e in service.filter_by_method("POST")] == ["POST"]
    assert service.filter_by_method("delete") == []


def test_filter_by_tag_is_case_insensitive_exact() -> None:
    service = _tagged_service()
    assert len(service.filter_by_tag("users")) == 2
    assert len(service.filter_by_tag("USERS")) == 2  # case-insensitive
    assert [e["method"] for e in service.filter_by_tag("admin")] == ["POST"]
    assert service.filter_by_tag("user") == []  # exact, not substring
    assert service.filter_by_tag("nope") == []


def test_search_endpoints_over_method_path_handler_tags() -> None:
    service = _tagged_service()
    assert {e["handler"] for e in service.search_endpoints("get_user")} == {"get_user"}
    assert len(service.search_endpoints("admin")) == 1  # matches a tag
    assert len(service.search_endpoints("/api/users")) == 2  # matches resolved path
    assert len(service.search_endpoints("POST")) == 1  # matches method, case-insensitive
    assert len(service.search_endpoints("")) == 2  # empty query = all (no filter)
    assert service.search_endpoints("zzz") == []


def test_list_config_aggregates_env_and_settings() -> None:
    sources = {
        "app/config.py": (
            "import os\n"
            "from pydantic_settings import BaseSettings\n"
            "DB = os.getenv('DATABASE_URL', 'sqlite://')\n"
            "class Settings(BaseSettings):\n"
            "    host: str = 'localhost'\n"
        )
    }
    config = {c["name"]: c for c in GraphService.from_sources(sources).list_config()}
    assert config["DATABASE_URL"]["kind"] == "env"
    assert config["DATABASE_URL"]["file"] == "app/config.py"
    assert config["DATABASE_URL"]["confidence"] == "static-certain"
    assert config["host"]["kind"] == "setting"


def test_get_endpoint_by_id_roundtrips(fastapi_sources: dict[str, str]) -> None:
    service = GraphService.from_sources(fastapi_sources)
    some_id = service.list_endpoints()[0]["id"]
    ep = service.get_endpoint_by_id(some_id)
    assert ep is not None and ep["id"] == some_id
    assert service.get_endpoint_by_id("nope:does:not:exist") is None


def test_from_directory_handles_bom_encoded_files(tmp_path) -> None:
    # Windows tooling (e.g. PowerShell Set-Content -Encoding utf8) writes a BOM.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ping')\ndef ping(): ...\n",
        encoding="utf-8-sig",
    )
    service = GraphService.from_directory(str(tmp_path))
    assert service.get_endpoint("GET", "/ping") is not None
