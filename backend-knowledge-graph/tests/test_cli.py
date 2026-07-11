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
