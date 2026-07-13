"""The bkg CLI end-to-end over a real on-disk project."""

from __future__ import annotations

import json

import pytest

from bkg.cli import main


def test_endpoints_json(fastapi_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoints", fastapi_project, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(e["resolved_path"] == "/api/users/{user_id}" for e in data)
    assert any(e["method"] == "POST" and e["resolved_path"] == "/api/users/" for e in data)


def test_endpoints_table(fastapi_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoints", fastapi_project])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GET" in out
    assert "/api/users/{user_id}" in out
    assert "get_user" in out


def test_endpoint_lookup(fastapi_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoint", fastapi_project, "GET", "/api/users/{user_id}"])
    assert rc == 0
    ep = json.loads(capsys.readouterr().out)
    assert ep["handler"] == "get_user"


def test_endpoint_not_found(fastapi_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoint", fastapi_project, "GET", "/missing"])
    assert rc == 1


def test_schemas(fastapi_dto_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["schemas", fastapi_dto_project, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(s["id"] == "app/schemas.py:UserCreate" for s in data)


def test_blast(fastapi_dto_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["blast", fastapi_dto_project, "app/schemas.py:UserCreate"])
    assert rc == 0
    assert "POST:/api/users/" in capsys.readouterr().out


def test_blast_no_match(fastapi_dto_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["blast", fastapi_dto_project, "app/schemas.py:Missing"])
    assert rc == 1


def test_trust(fastapi_dto_project: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["trust", fastapi_dto_project])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["endpoints"] == 2
    assert "by_confidence" in data


def test_config_command(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "config.py").write_text(
        "import os\nDB = os.getenv('DATABASE_URL', 'sqlite://')\n", encoding="utf-8"
    )
    rc = main(["config", str(tmp_path), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "DATABASE_URL" and c["kind"] == "env" for c in data)


def _write_tagged_project(tmp_path) -> str:
    (tmp_path / "app" / "routers").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "routers" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/api/users')\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "routers" / "users.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/{user_id}', tags=['users'])\n"
        "def get_user(user_id: int): ...\n"
        "@router.post('/', tags=['admin'])\n"
        "def create_user(): ...\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_endpoints_filter_by_method(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoints", _write_tagged_project(tmp_path), "--method", "get", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert {e["method"] for e in data} == {"GET"}


def test_endpoints_filter_by_tag(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoints", _write_tagged_project(tmp_path), "--tag", "admin", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["method"] for e in data] == ["POST"]


def test_search_command(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["search", _write_tagged_project(tmp_path), "admin", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1 and data[0]["method"] == "POST"


def test_endpoints_table_shows_tags(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["endpoints", _write_tagged_project(tmp_path)])
    assert rc == 0
    assert "[users]" in capsys.readouterr().out
