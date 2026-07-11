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


def test_from_directory_handles_bom_encoded_files(tmp_path) -> None:
    # Windows tooling (e.g. PowerShell Set-Content -Encoding utf8) writes a BOM.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ping')\ndef ping(): ...\n",
        encoding="utf-8-sig",
    )
    service = GraphService.from_directory(str(tmp_path))
    assert service.get_endpoint("GET", "/ping") is not None
