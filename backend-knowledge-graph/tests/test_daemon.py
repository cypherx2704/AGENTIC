"""The daemon keeps the graph in sync with on-disk changes, incrementally — and
every incremental result must equal a from-scratch rebuild of the same tree."""

from __future__ import annotations

import os

from bkg.daemon import Daemon
from bkg.service import GraphService


def _fresh_digest(root: str) -> str:
    return GraphService.from_directory(root).snapshot_digest()


def test_daemon_loads_directory(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    assert daemon.service.get_endpoint("GET", "/api/users/{user_id}") is not None
    assert daemon.service.snapshot_digest() == _fresh_digest(fastapi_project)


def test_modify_event_updates_incrementally(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    users = os.path.join(fastapi_project, "app", "routers", "users.py")
    with open(users, "a", encoding="utf-8") as f:
        f.write("\n@router.delete('/{user_id}')\ndef delete_user(user_id: int): ...\n")

    daemon.apply_events([("modify", users)])
    assert daemon.service.get_endpoint("DELETE", "/api/users/{user_id}") is not None
    assert daemon.service.snapshot_digest() == _fresh_digest(fastapi_project)  # incremental == rebuild


def test_add_event_introduces_a_new_router(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    new_router = os.path.join(fastapi_project, "app", "routers", "items.py")
    with open(new_router, "w", encoding="utf-8") as f:
        f.write(
            "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/')\ndef list_items(): ...\n"
        )
    main = os.path.join(fastapi_project, "app", "main.py")
    with open(main, "a", encoding="utf-8") as f:
        f.write("from app.routers import items\napp.include_router(items.router, prefix='/api/items')\n")

    daemon.apply_events([("add", new_router), ("modify", main)])
    assert daemon.service.get_endpoint("GET", "/api/items/") is not None
    assert daemon.service.snapshot_digest() == _fresh_digest(fastapi_project)


def test_resync_handles_a_deleted_file(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    os.remove(os.path.join(fastapi_project, "app", "routers", "users.py"))

    daemon.resync()  # must not crash even though main.py still imports the removed module
    assert all("users.py" not in e["handler_file"] for e in daemon.service.list_endpoints())
    assert daemon.service.snapshot_digest() == _fresh_digest(fastapi_project)
