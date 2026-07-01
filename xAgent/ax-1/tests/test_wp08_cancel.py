"""WP08 — DELETE /v1/tasks/{id} cooperative cancel + pipeline cancel observation.

The cancel endpoint's status matrix is decided semantics:
  * 202 — non-terminal task, cancel signal set on a CONFIGURED Valkey;
  * 409 — task already terminal (nothing to cancel);
  * 404 — unknown / cross-tenant task (RLS hides it — surfaces as NOT_FOUND);
  * 503 — no CONFIGURED Valkey (no signal channel) OR a configured Valkey that errors.

The conftest ``client`` fixture swaps in a network-free ``_FakeValkey`` that LACKS the
WP08 helper methods, so by default cancel is the 503 (no-store) case. To drive the 202 /
configured-error paths we inject a small in-memory fake that DOES implement the WP08
``set_cancel_signal`` (and the sibling helpers) so ``_valkey_client`` treats it as
configured. The DB read is monkeypatched on ``tasks_repo.get_task`` (no Postgres).

The final test drives the REAL Pipeline runner with a cancel signal pre-set and asserts
the run ends ``cancelled`` and the EVENT stage sees that terminal status — proving the
cooperative-cancel observation between stages.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.pipeline import Pipeline, PipelineContext, Stage
from agent_runtime.db import tasks_repo
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# ── In-memory fake Valkey implementing the WP08 helper interface ────────────────────
class FakeWp08Valkey:
    """A configured-Valkey double: it implements every WP08 helper the code calls.

    Presence of ``set_cancel_signal`` is what makes ``api.tasks._valkey_client`` treat
    this as a CONFIGURED client (vs the conftest fake, which lacks it). Each helper can be
    told to raise so the fail-closed / 503 paths are exercised.
    """

    def __init__(self, *, raise_on_set: Exception | None = None) -> None:
        self.store: dict[str, str] = {}
        self.raise_on_set = raise_on_set
        self.set_calls: list[tuple[str, str]] = []
        self.cleared: list[tuple[str, str]] = []

    @staticmethod
    def _cancel_key(prefix: str, tenant_id: str, task_id: str) -> str:
        return f"{prefix}cancel:{tenant_id}:{task_id}"

    async def set_cancel_signal(
        self, *, prefix: str, tenant_id: str, task_id: str, ttl_seconds: int, timeout_seconds: float
    ) -> None:
        if self.raise_on_set is not None:
            raise self.raise_on_set
        key = self._cancel_key(prefix, tenant_id, task_id)
        self.store[key] = "1"
        self.set_calls.append((tenant_id, task_id))

    async def is_cancelled(
        self, *, prefix: str, tenant_id: str, task_id: str, timeout_seconds: float
    ) -> bool:
        return self._cancel_key(prefix, tenant_id, task_id) in self.store

    async def clear_cancel_signal(
        self, *, prefix: str, tenant_id: str, task_id: str, timeout_seconds: float
    ) -> None:
        self.cleared.append((tenant_id, task_id))
        self.store.pop(self._cancel_key(prefix, tenant_id, task_id), None)

    async def aclose(self) -> None:
        # The app lifespan teardown calls app.state.valkey.aclose(); provide a no-op so
        # swapping this double onto app.state never breaks shutdown.
        return None


def _task_row(status: str) -> TaskRow:
    return TaskRow(
        task_id=TASK_ID,
        agent_id=TEST_AGENT,
        tenant_id=TEST_TENANT,
        trace_id=TRACE_ID,
        status=status,
        input={"message": "hi"},
    )


def _patch_get_task(monkeypatch: Any, row: TaskRow | None) -> None:
    async def _fake_get_task(pool: Any, tenant_id: str, task_id: str) -> TaskRow | None:
        assert tenant_id == TEST_TENANT  # RLS scope comes from the JWT
        return row

    monkeypatch.setattr(tasks_repo, "get_task", _fake_get_task)


# ── 202: running task + configured Valkey -> cancel signal set ──────────────────────
async def test_cancel_running_task_returns_202_and_sets_signal(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()  # repos are patched; pool is a handle only
    fake_vk = FakeWp08Valkey()
    app.state.valkey = fake_vk
    _patch_get_task(monkeypatch, _task_row("running"))

    resp = await client.delete(f"/v1/tasks/{TASK_ID}")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["task_id"] == TASK_ID
    assert body["status"] == "cancel_requested"
    # The cancel signal actually landed in the store for this (tenant, task).
    assert fake_vk.set_calls == [(TEST_TENANT, TASK_ID)]


# ── 409: terminal task -> conflict ──────────────────────────────────────────────────
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "timeout"])
async def test_cancel_terminal_task_returns_409(client, monkeypatch: Any, terminal: str) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    fake_vk = FakeWp08Valkey()
    app.state.valkey = fake_vk
    _patch_get_task(monkeypatch, _task_row(terminal))

    resp = await client.delete(f"/v1/tasks/{TASK_ID}")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    # No signal set for an already-terminal task.
    assert fake_vk.set_calls == []


# ── 404: unknown / cross-tenant task (RLS-hidden) -> not found ───────────────────────
async def test_cancel_unknown_task_returns_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    app.state.valkey = FakeWp08Valkey()
    _patch_get_task(monkeypatch, None)  # RLS hides cross-tenant rows -> None

    resp = await client.delete(f"/v1/tasks/{TASK_ID}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── 503: no CONFIGURED Valkey (conftest fake lacks set_cancel_signal) ───────────────
async def test_cancel_without_configured_valkey_returns_503(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    # Leave the conftest network-free _FakeValkey in place: it has NO set_cancel_signal,
    # so _valkey_client() returns None -> no signal channel -> 503.
    assert not hasattr(app.state.valkey, "set_cancel_signal")
    _patch_get_task(monkeypatch, _task_row("running"))

    resp = await client.delete(f"/v1/tasks/{TASK_ID}")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── 503: configured Valkey that ERRORS on set -> cannot guarantee -> 503 ─────────────
async def test_cancel_store_error_returns_503(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    app.state.valkey = FakeWp08Valkey(raise_on_set=RuntimeError("valkey down"))
    _patch_get_task(monkeypatch, _task_row("running"))

    resp = await client.delete(f"/v1/tasks/{TASK_ID}")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── 503: no task store (pool None) -> service unavailable (covers the pool guard) ────
async def test_cancel_without_pool_returns_503(client) -> None:  # type: ignore[no-untyped-def]
    # The conftest client already nulls db_pool. No DB -> task store unavailable.
    assert client._transport.app.state.db_pool is None
    resp = await client.delete(f"/v1/tasks/{TASK_ID}")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── Pipeline observation: a pre-set cancel signal ends the run 'cancelled' ───────────
class _SpyEventStage(Stage):
    """EVENT stand-in that records the terminal status the runner finalised with."""

    name = "EVENT"

    def __init__(self) -> None:
        self.ran = False
        self.terminal_status = ""

    async def run(self, ctx: PipelineContext) -> None:
        self.ran = True
        self.terminal_status = ctx.terminal_error.status if ctx.terminal_error else "completed"


def _principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["agent:execute"],
        raw_token="agent.jwt",
    )


def _ctx_with_cancel(cancel_set: bool) -> PipelineContext:
    fake_vk = FakeWp08Valkey()
    from agent_runtime.core.config import get_settings

    s = get_settings()
    if cancel_set:
        key = FakeWp08Valkey._cancel_key(s.task_signal_key_prefix, TEST_TENANT, TASK_ID)
        fake_vk.store[key] = "1"

    async def _check() -> bool:
        return await fake_vk.is_cancelled(
            prefix=s.task_signal_key_prefix,
            tenant_id=TEST_TENANT,
            task_id=TASK_ID,
            timeout_seconds=s.task_signal_valkey_timeout_seconds,
        )

    return PipelineContext(
        principal=_principal(),
        inbound_agent_jwt="agent.jwt",
        trace_id=TRACE_ID,
        request_id="req-1",
        task=_task_row("running"),
        steps=StepBuffer(),
        started_monotonic=time.monotonic(),
        started_at="2026-06-10T12:00:00.000Z",
        cancel_check=_check,
    )


@pytest.mark.asyncio
async def test_pipeline_cancel_signal_pre_set_ends_cancelled() -> None:
    """A cancel signal observed BETWEEN stages short-circuits to a 'cancelled' terminal.

    We bind a single stage that should NEVER run (the cancel is observed before it) so we
    prove the runner checks the signal at the stage boundary and finalises 'cancelled'.
    """
    from agent_runtime.core import pipeline as pipeline_mod
    from agent_runtime.core.pipeline import StageSpec

    ran: list[str] = []

    class _NeverRunsStage(Stage):
        name = "LOAD"

        async def run(self, ctx: PipelineContext) -> None:
            ran.append("LOAD")

    original = [StageSpec(s.name, s.enabled, s.stage_cls) for s in pipeline_mod.STAGE_REGISTRY]
    # Disable every slot except a single LOAD stand-in so the run is deterministic.
    for spec in pipeline_mod.STAGE_REGISTRY:
        spec.enabled = spec.name == "LOAD"
    pipeline_mod.bind_stage("LOAD", _NeverRunsStage)
    try:
        event = _SpyEventStage()
        ctx = _ctx_with_cancel(cancel_set=True)
        result = await Pipeline(stages=[_NeverRunsStage()], event_stage=event).run(ctx)
    finally:
        pipeline_mod.STAGE_REGISTRY[:] = original

    assert result.terminal_error is not None
    assert result.terminal_error.status == "cancelled"
    assert event.ran is True
    assert event.terminal_status == "cancelled"
    # The cancel was observed at the FIRST boundary, before LOAD executed.
    assert ran == []


@pytest.mark.asyncio
async def test_pipeline_no_cancel_signal_runs_normally() -> None:
    """Sanity: with no cancel signal the bound stage runs and the task completes."""
    ran: list[str] = []

    class _OkStage(Stage):
        name = "LOAD"

        async def run(self, ctx: PipelineContext) -> None:
            ran.append("LOAD")

    event = _SpyEventStage()
    ctx = _ctx_with_cancel(cancel_set=False)
    result = await Pipeline(stages=[_OkStage()], event_stage=event).run(ctx)

    assert result.terminal_error is None
    assert event.terminal_status == "completed"
    assert ran == ["LOAD"]
