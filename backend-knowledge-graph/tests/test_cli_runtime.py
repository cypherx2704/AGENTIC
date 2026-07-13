"""The `bkg runtime` CLI reconciles an observations JSON file with the graph."""

from __future__ import annotations

import json

import pytest

from bkg.cli import main


def test_runtime_command(fastapi_dto_project: str, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    obs = tmp_path / "obs.json"
    obs.write_text(
        json.dumps([{"method": "GET", "path": "/api/users/1"}, {"method": "GET", "path": "/api/nope"}]),
        encoding="utf-8",
    )
    rc = main(["runtime", fastapi_dto_project, str(obs), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["confirmed"] == 1
    assert any(ro["path"] == "/api/nope" for ro in data["runtime_only"])
