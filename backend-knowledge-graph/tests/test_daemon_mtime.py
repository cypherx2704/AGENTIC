"""mtime-aware resync: unchanged files are skipped, changed files are re-read, and
the incremental result still equals a from-scratch rebuild."""

from __future__ import annotations

import os

from bkg.daemon import Daemon
from bkg.service import GraphService


def test_resync_noop_when_nothing_changed(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    before = daemon.service.snapshot_digest()
    daemon.resync()
    assert daemon.service.snapshot_digest() == before


def test_resync_skips_unchanged_files(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    reads: list[str] = []
    original = daemon.service.update_file

    def spy(path: str, text: str) -> None:
        reads.append(path)
        original(path, text)

    daemon.service.update_file = spy  # type: ignore[method-assign]
    daemon.resync()
    assert reads == []  # no file changed on disk -> nothing re-read


def test_resync_reflects_a_changed_file(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    users = os.path.join(fastapi_project, "app", "routers", "users.py")
    with open(users, "a", encoding="utf-8") as f:
        f.write("\n@router.delete('/{user_id}')\ndef delete_user(user_id: int): ...\n")

    daemon.resync()  # the size changed -> detected regardless of mtime granularity
    assert daemon.service.get_endpoint("DELETE", "/api/users/{user_id}") is not None
    assert daemon.service.snapshot_digest() == GraphService.from_directory(fastapi_project).snapshot_digest()


def test_resync_detects_same_mtime_different_content(fastapi_project: str) -> None:
    daemon = Daemon(fastapi_project)
    users = os.path.join(fastapi_project, "app", "routers", "users.py")
    sig = daemon._sigs["app/routers/users.py"]
    assert sig is not None
    # rewrite with DIFFERENT content, then pin mtime back (git-checkout / tar --times style)
    with open(users, "w", encoding="utf-8") as f:
        f.write(
            "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/renamed')\ndef g(): ...\n"
        )
    os.utime(users, (sig[0], sig[0]))  # mtime preserved; only size differs

    daemon.resync()
    assert daemon.service.get_endpoint("GET", "/api/users/renamed") is not None
    assert daemon.service.snapshot_digest() == GraphService.from_directory(fastapi_project).snapshot_digest()
