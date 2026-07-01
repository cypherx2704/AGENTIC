"""WP12 — async task mode (POST /v1/tasks?mode=async).

Drives the REAL endpoint via the conftest ``client`` fixture (auth overridden, network-free
Valkey). The DB repos are monkeypatched (no Postgres) and ``Pipeline.from_registry`` is
swapped for a controllable fake so the background run is deterministic + fast.

Coverage:
  * ?mode=async -> 202 + task_id (status running, mode async), and the background pipeline
    actually runs to a terminal state via the existing run mechanism (asserted by waiting on
    the tracked background task + a recording fake pipeline);
  * missing Idempotency-Key on async -> 422 BEFORE any persistence (no orphan row);
  * async disabled by settings -> 422;
  * ?mode=sync (default) is unchanged: runs inline, returns 200.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runtime.api import tasks as tasks_api
from agent_runtime.core.config import get_settings
from agent_runtime.db import tasks_repo
from agent_runtime.db.tasks_repo import TaskRow

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "33333333-3333-3333-3333-333333333333"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


def _task_row(status: str = "pending") -> TaskRow:
    return TaskRow(task_id=TASK_ID, agent_id=TEST_AGENT, tenant_id=TEST_TENANT,
                   trace_id=TRACE_ID, status=status, input={"message": "hi"})


def _patch_repos(monkeypatch: Any) -> None:
    """Stub create_task / mark_running so no Postgres is touched + skip the authorize call.

    The authorize layer-B check (``task:execute``) otherwise makes a live HTTP call to the
    Auth service (localhost), which 403s under test. We disable it on the cached Settings —
    the authorize seam is exercised by the WP08 authorize suite, not here.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "authorize_enabled", False, raising=False)

    async def _create(pool: Any, **kw: Any) -> TaskRow:
        return _task_row("pending")

    async def _mark_running(pool: Any, tenant_id: str, task_id: str) -> None:
        return None

    monkeypatch.setattr(tasks_repo, "create_task", _create)
    monkeypatch.setattr(tasks_repo, "mark_running", _mark_running)


def _patch_pipeline(monkeypatch: Any, *, final_answer: str = "done") -> dict[str, Any]:
    """Swap Pipeline.from_registry for a fake that records the run + sets a final answer.

    Returns a dict ``state`` the test inspects (``state['ran']`` flips True once the fake
    pipeline executed — proving the background driver actually ran the pipeline).
    """
    state: dict[str, Any] = {"ran": False, "ctx": None}

    class _FakePipeline:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        @classmethod
        def from_registry(cls, _event_stage: Any) -> _FakePipeline:
            return cls()

        async def run(self, ctx: Any) -> Any:
            state["ran"] = True
            state["ctx"] = ctx
            ctx.final_answer = final_answer
            return ctx

    monkeypatch.setattr(tasks_api, "Pipeline", _FakePipeline)
    return state


async def _drain_background(app: Any) -> None:
    """Await any tracked fire-and-forget async-run tasks so assertions are deterministic."""
    pending = getattr(app.state, "_async_task_runs", set())
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)


# ── 202 + background run ─────────────────────────────────────────────────────────────
async def test_async_mode_returns_202_and_runs_in_background(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()  # repos patched; pool is a handle only
    _patch_repos(monkeypatch)
    state = _patch_pipeline(monkeypatch, final_answer="async answer")

    resp = await client.post(
        "/v1/tasks?mode=async",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}},
        headers={"Idempotency-Key": "async-key-1"},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["task_id"] == TASK_ID
    assert body["status"] == "running"
    assert body["mode"] == "async"

    # The background pipeline actually ran to a terminal answer (existing run mechanism).
    await _drain_background(app)
    assert state["ran"] is True
    assert state["ctx"].final_answer == "async answer"


# ── missing Idempotency-Key on async -> 422 (before persistence) ────────────────────
async def test_async_without_idempotency_key_is_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()

    created: list[bool] = []

    async def _create(pool: Any, **kw: Any) -> TaskRow:
        created.append(True)
        return _task_row()

    monkeypatch.setattr(tasks_repo, "create_task", _create)

    resp = await client.post(
        "/v1/tasks?mode=async",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}},
        # no Idempotency-Key header
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "ASYNC_REQUIRES_IDEMPOTENCY_KEY"
    # The 422 fired BEFORE any persistence -> no orphan task row created.
    assert created == []


# ── async disabled by settings -> 422 ───────────────────────────────────────────────
async def test_async_disabled_is_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    settings = get_settings()
    monkeypatch.setattr(settings, "async_mode_enabled", False, raising=False)

    resp = await client.post(
        "/v1/tasks?mode=async",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}},
        headers={"Idempotency-Key": "k"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "ASYNC_MODE_DISABLED"


# ── bad mode value -> 422 ────────────────────────────────────────────────────────────
async def test_bad_mode_value_is_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()

    resp = await client.post(
        "/v1/tasks?mode=banana",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["reason"] == "BAD_MODE"


# ── default sync mode is unchanged: runs inline, 200 ────────────────────────────────
async def test_sync_mode_runs_inline_returns_200(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    _patch_repos(monkeypatch)
    state = _patch_pipeline(monkeypatch, final_answer="sync answer")

    resp = await client.post(
        "/v1/tasks",  # no ?mode -> sync default
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"] == {"message": "sync answer"}
    # Sync ran inline (no background task needed).
    assert state["ran"] is True
