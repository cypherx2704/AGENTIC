"""WP08 — backup TaskSweeper.sweep_once + the per-task timeout wrapper.

Two surfaces:
  1. ``TaskSweeper.sweep_once`` against a fake pool returning one stuck task: asserts the
     atomic failed-finalize (``outbox.sweep_task_failed``) is issued for that task AND the
     retention deletes (``delete_old_task_steps`` / ``delete_old_outbox``) are issued; plus
     the ``pool=None`` quiet no-op and the fail-soft batch (one bad row never aborts).
  2. The per-task timeout path: a slow stage under a tiny ``asyncio.timeout`` budget makes
     the runner raise ``TimeoutError``; the api layer marks the task ``timeout`` and runs
     EVENT. We drive the real submit endpoint with a settings override shrinking the budget
     and a slow pipeline double, asserting the response status is ``timeout``.

The sweeper's DB layer (``list_stuck_tasks`` / ``sweep_task_failed`` / the retention
deletes) is monkeypatched — this is a wrapper-logic test, not a live-SQL test (the SQL +
RLS bypass + atomicity are an honest gap, exercised only against a real Postgres).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.core.config import get_settings
from agent_runtime.db import outbox, tasks_repo
from agent_runtime.db.tasks_repo import StuckTask
from agent_runtime.services.sweeper import TaskSweeper

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


# ── A minimal recording fake pool/conn (enough for the sweeper's two transactions) ──
class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.executed.append(sql.strip())
        return None

    def transaction(self) -> _AsyncCtx:
        return _AsyncCtx(self)

    def cursor(self, *_a: Any, **_k: Any) -> Any:  # not used (repo fns are patched)
        raise AssertionError("cursor() should not be reached — repo fns are monkeypatched")


class _AsyncCtx:
    """Serve as both the connection() and transaction() async context manager."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def connection(self, *_a: Any, **_k: Any) -> _AsyncCtx:
        return _AsyncCtx(self.conn)


def _stuck(task_id: str) -> StuckTask:
    return StuckTask(
        task_id=task_id,
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        trace_id="trace",
        status="running",
    )


# ── sweep_once: stuck task finalised + retention issued ─────────────────────────────
@pytest.mark.asyncio
async def test_sweep_once_finalises_stuck_and_runs_retention(monkeypatch: Any) -> None:
    pool = _FakePool()
    sweeper = TaskSweeper(pool, get_settings())  # type: ignore[arg-type]

    swept: list[str] = []
    retention: dict[str, int] = {}

    async def _fake_list_stuck(conn: Any, *, grace_seconds: int, limit: int) -> list[StuckTask]:
        return [_stuck("task-A")]

    async def _fake_sweep_failed(pool_arg: Any, *, task_id: str, **kw: Any) -> bool:
        swept.append(task_id)
        return True  # the sweeper actually finalised this row

    async def _fake_del_steps(conn: Any, *, retention_days: int) -> int:
        retention["steps"] = retention_days
        return 3

    async def _fake_del_outbox(conn: Any, *, retention_days: int) -> int:
        retention["outbox"] = retention_days
        return 5

    monkeypatch.setattr(tasks_repo, "list_stuck_tasks", _fake_list_stuck)
    monkeypatch.setattr(outbox, "sweep_task_failed", _fake_sweep_failed)
    monkeypatch.setattr(tasks_repo, "delete_old_task_steps", _fake_del_steps)
    monkeypatch.setattr(tasks_repo, "delete_old_outbox", _fake_del_outbox)

    await sweeper.sweep_once()

    # The stuck task was finalised atomically failed; both retention deletes ran.
    assert swept == ["task-A"]
    assert retention == {"steps": get_settings().task_steps_retention_days,
                         "outbox": get_settings().outbox_retention_days}
    # Discovery + retention each set the sweeper RLS-bypass GUC inside their transaction.
    sweeper_gucs = [s for s in pool.conn.executed if "app.sweeper" in s]
    assert len(sweeper_gucs) == 2


# ── sweep_once with pool=None is a quiet no-op (tests / degraded) ───────────────────
@pytest.mark.asyncio
async def test_sweep_once_no_pool_is_noop() -> None:
    sweeper = TaskSweeper(None, get_settings())
    await sweeper.sweep_once()  # must not raise / touch anything


# ── one bad row must not abort the batch (fail-soft finalize) ───────────────────────
@pytest.mark.asyncio
async def test_sweep_finalize_failure_is_fail_soft(monkeypatch: Any) -> None:
    pool = _FakePool()
    sweeper = TaskSweeper(pool, get_settings())  # type: ignore[arg-type]
    swept: list[str] = []

    async def _fake_list_stuck(conn: Any, **_kw: Any) -> list[StuckTask]:
        return [_stuck("bad"), _stuck("good")]

    async def _fake_sweep_failed(pool_arg: Any, *, task_id: str, **kw: Any) -> bool:
        if task_id == "bad":
            raise RuntimeError("tenant tx failed")
        swept.append(task_id)
        return True

    async def _noop_del(conn: Any, *, retention_days: int) -> int:
        return 0

    monkeypatch.setattr(tasks_repo, "list_stuck_tasks", _fake_list_stuck)
    monkeypatch.setattr(outbox, "sweep_task_failed", _fake_sweep_failed)
    monkeypatch.setattr(tasks_repo, "delete_old_task_steps", _noop_del)
    monkeypatch.setattr(tasks_repo, "delete_old_outbox", _noop_del)

    await sweeper.sweep_once()  # must not raise despite the 'bad' row

    # The 'bad' row was skipped; the 'good' row was still finalised.
    assert swept == ["good"]


# ── retention failure is swallowed (best-effort) ────────────────────────────────────
@pytest.mark.asyncio
async def test_sweep_retention_failure_is_swallowed(monkeypatch: Any) -> None:
    pool = _FakePool()
    sweeper = TaskSweeper(pool, get_settings())  # type: ignore[arg-type]

    async def _no_stuck(conn: Any, **_kw: Any) -> list[StuckTask]:
        return []

    async def _boom_del(conn: Any, *, retention_days: int) -> int:
        raise RuntimeError("delete failed")

    monkeypatch.setattr(tasks_repo, "list_stuck_tasks", _no_stuck)
    monkeypatch.setattr(tasks_repo, "delete_old_task_steps", _boom_del)
    monkeypatch.setattr(tasks_repo, "delete_old_outbox", _boom_del)

    await sweeper.sweep_once()  # retention error logged + swallowed, never raised


# ── Per-task timeout wrapper: a slow run overruns the budget -> 'timeout' status ────
class _SlowPipeline:
    """Pipeline double whose run() sleeps past the (shrunk) task budget."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    @classmethod
    def from_registry(cls, _event_stage: Any) -> _SlowPipeline:
        return cls()

    async def run(self, ctx: Any) -> Any:
        import asyncio

        await asyncio.sleep(5)  # far longer than the 0.05s budget -> TimeoutError upstream
        return ctx


async def test_submit_timeout_marks_task_timeout(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    """Drive POST /v1/tasks with a 0.05s budget + a slow pipeline -> 'timeout' response.

    The timeout finalise runs the REAL EventStage, but the pool is nulled (conftest), so
    EVENT no-ops the DB write — the response is still assembled from ctx with status
    'timeout'. This proves the asyncio.timeout wrapper + _finalise_after_timeout path.
    """
    from agent_runtime.api import tasks as tasks_api

    app = client._transport.app
    app.state.db_pool = object()

    async def _fake_create(pool: Any, **kw: Any) -> Any:
        return tasks_repo.TaskRow(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_id=TEST_AGENT,
            tenant_id=TEST_TENANT,
            trace_id="trace",
            status="pending",
            input={"message": "hi"},
        )

    async def _fake_mark_running(pool: Any, tenant_id: str, task_id: str) -> None:
        return None

    monkeypatch.setattr(tasks_repo, "create_task", _fake_create)
    monkeypatch.setattr(tasks_repo, "mark_running", _fake_mark_running)
    monkeypatch.setattr(tasks_api, "Pipeline", _SlowPipeline)

    # Shrink the budget + disable authorize/idempotency so only the timeout path runs.
    settings = get_settings().model_copy(
        update={"task_timeout_seconds": 1, "authorize_enabled": False, "idempotency_enabled": False}
    )
    monkeypatch.setattr(tasks_api, "get_settings", lambda: settings)

    # The effective budget is min(task_timeout_seconds, body.timeout_seconds); send a tiny
    # caller timeout so the wrapper trips quickly without a multi-second test.
    resp = await client.post(
        "/v1/tasks",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}, "mode": "sync", "timeout_seconds": 1},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "timeout"
