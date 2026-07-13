"""File-backed persistence (local-first): the daemon/MCP keep the graph in
``<root>/.bkg/graph.db`` so a restart reopens it warm instead of rebuilding, and
a file deleted while the daemon was DOWN is still reaped on the next start. The
one-shot CLI stays ephemeral (:memory:)."""

from __future__ import annotations

import os

from bkg.daemon import Daemon, default_db_path
from bkg.service import GraphService


def test_default_db_is_written_and_self_ignored(fastapi_project: str) -> None:
    Daemon(fastapi_project)
    db = os.path.join(fastapi_project, ".bkg", "graph.db")
    ignore = os.path.join(fastapi_project, ".bkg", ".gitignore")
    assert os.path.exists(db)
    with open(ignore, encoding="utf-8") as handle:
        assert handle.read() == "*\n"  # the whole .bkg dir is git-ignored


def test_graph_persists_and_reopens_warm_across_restart(fastapi_project: str) -> None:
    first = Daemon(fastapi_project)
    endpoints = first.service.list_endpoints()
    digest = first.service.snapshot_digest()
    assert endpoints  # built once

    # simulate a restart: a NEW daemon over the SAME persisted .bkg/graph.db
    second = Daemon(fastapi_project)
    second.service._engine.reset_counters()
    reopened = second.service.list_endpoints()

    # served straight from the persisted graph — nothing was recomputed
    assert second.service._engine.recompute_count == 0
    assert second.service.snapshot_digest() == digest
    assert {e["id"] for e in reopened} == {e["id"] for e in endpoints}


def test_file_deleted_while_down_is_reaped_on_restart(fastapi_project: str) -> None:
    # build + persist a graph that includes the routes file
    first = Daemon(fastapi_project)
    assert any(e["handler_file"] == "app/routers/users.py" for e in first.service.list_endpoints())

    # delete the routes file while the "daemon is down" (no event delivered)
    os.remove(os.path.join(fastapi_project, "app", "routers", "users.py"))

    # restart: the manifest is recovered from the store, so the vanished file is reaped
    second = Daemon(fastapi_project)
    assert second.service.list_endpoints() == []
    # byte-identical to a from-scratch rebuild of the current (reduced) tree
    assert second.service.snapshot_digest() == GraphService.from_directory(fastapi_project).snapshot_digest()


def test_cli_path_stays_ephemeral(fastapi_project: str) -> None:
    # GraphService.from_directory (what the one-shot CLI uses) must NOT persist
    GraphService.from_directory(fastapi_project).list_endpoints()
    assert not os.path.exists(os.path.join(fastapi_project, ".bkg"))


def test_default_db_path_points_into_dot_bkg(fastapi_project: str) -> None:
    path = default_db_path(fastapi_project)
    assert path == os.path.join(fastapi_project, ".bkg", "graph.db")
