"""WP08 — GET /v1/tasks list (Task Feed): cursor pagination + filters + redaction.

Contract: ``{ "tasks": [<redaction-safe summary>...], "next_cursor": <opaque|null> }``.
Each summary carries ids/status/usage/timestamps/error_code/metadata but NEVER the
free-form ``input`` / ``output`` / ``error_msg``. RLS scopes every row to the JWT tenant.

The repo (``tasks_repo.list_tasks``) is monkeypatched to return canned ``TaskListItem``
rows (no DB). We assert: the endpoint requests ``limit + 1`` rows to detect a next page,
trims to ``limit`` and emits a ``next_cursor`` only when more rows exist; filters
(since/status/agent_id) and the decoded cursor are forwarded to the repo; the tenant_id
the repo is called with is the JWT tenant (RLS scoping); a bad status filter / malformed
cursor are 422; and the projection is redaction-safe.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from agent_runtime.api import tasks as tasks_api
from agent_runtime.db import tasks_repo
from agent_runtime.db.tasks_repo import TaskListItem

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _item(n: int, *, status: str = "completed") -> TaskListItem:
    return TaskListItem(
        task_id=f"task-{n:04d}",
        agent_id=TEST_AGENT,
        status=status,
        trace_id=f"trace-{n}",
        error_code=None,
        tokens_used=10 * n,
        cost_usd=0.001 * n,
        metadata={"campaign": "q2"},
        created_at=f"2026-06-10T12:00:{n:02d}.000Z",
        started_at=f"2026-06-10T12:00:{n:02d}.100Z",
        completed_at=f"2026-06-10T12:00:{n:02d}.500Z",
    )


class _Recorder:
    def __init__(self, rows: list[TaskListItem]) -> None:
        self.rows = rows
        self.kwargs: dict[str, Any] = {}
        self.tenant_id: str | None = None

    async def list_tasks(self, pool: Any, tenant_id: str, **kwargs: Any) -> list[TaskListItem]:
        self.tenant_id = tenant_id
        self.kwargs = kwargs
        # Honour the requested limit so has_more detection is realistic.
        return self.rows[: kwargs["limit"]]


def _install(client, monkeypatch: Any, rows: list[TaskListItem]) -> _Recorder:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    rec = _Recorder(rows)
    monkeypatch.setattr(tasks_repo, "list_tasks", rec.list_tasks)
    return rec


# ── first page with more rows -> next_cursor present ────────────────────────────────
async def test_list_first_page_emits_next_cursor(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    # 3 rows available; ask for limit=2. The endpoint fetches limit+1 (3), sees has_more.
    rec = _install(client, monkeypatch, [_item(1), _item(2), _item(3)])

    resp = await client.get("/v1/tasks?limit=2")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["tasks"]) == 2  # trimmed to the page size
    assert body["next_cursor"] is not None
    # The repo was asked for limit + 1 to detect the next page.
    assert rec.kwargs["limit"] == 3
    # RLS scoping: the repo is called with the JWT tenant.
    assert rec.tenant_id == TEST_TENANT

    # next_cursor decodes to the (created_at, task_id) of the LAST returned row.
    decoded = json.loads(base64.urlsafe_b64decode(body["next_cursor"]).decode())
    assert decoded["t"] == "task-0002"


# ── last page -> next_cursor null ───────────────────────────────────────────────────
async def test_list_last_page_has_null_cursor(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    rec = _install(client, monkeypatch, [_item(1), _item(2)])

    resp = await client.get("/v1/tasks?limit=5")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["tasks"]) == 2
    assert body["next_cursor"] is None  # fewer rows than limit -> last page
    assert rec.kwargs["limit"] == 6


# ── filters (since/status/agent_id) forwarded to the repo ───────────────────────────
async def test_list_filters_forwarded(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    rec = _install(client, monkeypatch, [_item(1, status="failed")])

    resp = await client.get(
        "/v1/tasks?status=failed&agent_id=" + TEST_AGENT + "&since=2026-06-10T00:00:00Z"
    )

    assert resp.status_code == 200, resp.text
    assert rec.kwargs["status"] == "failed"
    assert rec.kwargs["agent_id"] == TEST_AGENT
    assert rec.kwargs["since"] == "2026-06-10T00:00:00Z"


# ── cursor forwarded + decoded into (created_at, task_id) ───────────────────────────
async def test_list_cursor_decoded_and_forwarded(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    rec = _install(client, monkeypatch, [_item(4)])
    cursor = base64.urlsafe_b64encode(
        json.dumps({"c": "2026-06-10T12:00:03.000Z", "t": "task-0003"}).encode()
    ).decode()

    resp = await client.get(f"/v1/tasks?cursor={cursor}")

    assert resp.status_code == 200, resp.text
    assert rec.kwargs["cursor_created_at"] == "2026-06-10T12:00:03.000Z"
    assert rec.kwargs["cursor_task_id"] == "task-0003"


# ── bad status filter -> 422 ────────────────────────────────────────────────────────
async def test_list_bad_status_returns_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    _install(client, monkeypatch, [])
    resp = await client.get("/v1/tasks?status=bogus")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── malformed cursor -> 422 (never a silent first-page reset) ───────────────────────
async def test_list_bad_cursor_returns_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    _install(client, monkeypatch, [])
    resp = await client.get("/v1/tasks?cursor=not-base64!!!")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── redaction-safe projection: no input / output / error_msg ────────────────────────
async def test_list_projection_is_redaction_safe(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    _install(client, monkeypatch, [_item(1)])
    resp = await client.get("/v1/tasks")
    assert resp.status_code == 200, resp.text
    item = resp.json()["tasks"][0]
    # The envelope the Task Feed needs is present.
    for field in ("task_id", "agent_id", "status", "trace_id", "tokens_used", "cost_usd",
                  "metadata", "created_at", "started_at", "completed_at", "error_code"):
        assert field in item, field
    # The free-form / possibly-sensitive payloads are NEVER projected.
    assert "input" not in item
    assert "output" not in item
    assert "error_msg" not in item
    assert "error" not in item


# ── limit bounds enforced by FastAPI Query (le=200, ge=1) -> 422 ────────────────────
async def test_list_limit_over_cap_returns_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    _install(client, monkeypatch, [])
    resp = await client.get("/v1/tasks?limit=9999")
    assert resp.status_code == 422, resp.text


# ── no pool -> 503 ──────────────────────────────────────────────────────────────────
async def test_list_without_pool_returns_503(client) -> None:  # type: ignore[no-untyped-def]
    assert client._transport.app.state.db_pool is None  # conftest nulls it
    resp = await client.get("/v1/tasks")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── unit: the cursor codec round-trips ──────────────────────────────────────────────
def test_cursor_codec_roundtrip() -> None:
    item = _item(7)
    token = tasks_api._encode_cursor(item)
    created_at, task_id = tasks_api._decode_cursor(token)
    assert created_at == item.created_at
    assert task_id == item.task_id
    # An absent cursor decodes to (None, None).
    assert tasks_api._decode_cursor(None) == (None, None)
