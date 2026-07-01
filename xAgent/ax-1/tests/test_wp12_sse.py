"""WP12 — SSE stream endpoint (GET /v1/tasks/{id}/stream).

Drives the REAL endpoint via the conftest ``client`` fixture. DB repos are monkeypatched
(no Postgres). Three transports are exercised:
  * already-terminal row -> one ``snapshot`` frame + a terminal frame (``done`` /
    ``content_filter``), then close — verifies well-formed ``event:``/``data:`` framing;
  * NO configured Pub/Sub (conftest fake lacks ``subscribe_task_events``) -> the relay
    FALLS BACK to polling the row/steps, emitting ``snapshot`` frames until the task turns
    terminal (fail-soft, no infra);
  * a CONFIGURED Valkey exposing ``subscribe_task_events`` -> live frames are relayed and a
    terminal ``done`` frame closes the stream.
  * 404 for an unknown / cross-tenant task (RLS hides it); streaming-disabled -> 404.

The SSE poll interval is shrunk via the cached Settings so the fallback finishes instantly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agent_runtime.core.config import get_settings
from agent_runtime.db import steps_repo, tasks_repo
from agent_runtime.db.steps_repo import StepRow
from agent_runtime.db.tasks_repo import TaskRow

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "44444444-4444-4444-4444-444444444444"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


def _row(status: str, *, output: dict[str, Any] | None = None, error_code: str | None = None,
         error_msg: str | None = None) -> TaskRow:
    return TaskRow(
        task_id=TASK_ID, agent_id=TEST_AGENT, tenant_id=TEST_TENANT, trace_id=TRACE_ID,
        status=status, input={"message": "hi"}, output=output, error_code=error_code,
        error_msg=error_msg, created_at="2026-06-10T12:00:00.000Z",
        started_at="2026-06-10T12:00:00.000Z",
        completed_at="2026-06-10T12:00:01.000Z" if status != "running" else None,
    )


def _step(name: str = "llm_call", status: str = "passed") -> StepRow:
    return StepRow(task_id=TASK_ID, tenant_id=TEST_TENANT, step_type="llm_call",
                   step_name=name, status=status, duration_ms=2)


def _patch_steps(monkeypatch: Any, steps: list[StepRow] | None = None) -> None:
    async def _list(pool: Any, tenant_id: str, task_id: str) -> list[StepRow]:
        return list(steps or [])

    monkeypatch.setattr(steps_repo, "list_steps", _list)


def _parse_sse(text: str) -> list[dict[str, str]]:
    """Parse raw SSE text into a list of {event, data} frames (comment lines ignored)."""
    frames: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("event:"):
            cur["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            cur["data"] = line[len("data:"):].strip()
        elif line == "" and cur:
            frames.append(cur)
            cur = {}
    if cur:
        frames.append(cur)
    return frames


async def _collect(client: Any, url: str) -> str:
    """Read the full SSE body (the test streams close themselves on a terminal frame)."""
    async with client.stream("GET", url) as resp:
        assert resp.status_code == 200, resp
        chunks = [chunk async for chunk in resp.aiter_bytes()]
    return b"".join(chunks).decode("utf-8")


# ── already-terminal: snapshot + done frame, well-formed framing ────────────────────
async def test_completed_task_emits_snapshot_and_done(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    monkeypatch.setattr(tasks_repo, "get_task",
                        _async_const(_row("completed", output={"message": "42"})))
    _patch_steps(monkeypatch, [_step("guardrail_check_input"), _step("llm_call")])

    text = await _collect(client, f"/v1/tasks/{TASK_ID}/stream")
    frames = _parse_sse(text)

    events = [f["event"] for f in frames]
    assert events[0] == "snapshot"
    assert events[-1] == "done"  # completed -> terminal done frame
    # Well-formed: every frame carries both an event: and a JSON data: line.
    import json

    for f in frames:
        assert "event" in f and "data" in f
        json.loads(f["data"])  # valid JSON payload
    # The snapshot carries the ordered steps.
    snap = json.loads(frames[0]["data"])
    assert [s["step"] for s in snap["task_steps"]] == ["guardrail_check_input", "llm_call"]


# ── guardrail-blocked terminal -> content_filter frame ──────────────────────────────
async def test_guardrail_blocked_emits_content_filter(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    monkeypatch.setattr(
        tasks_repo, "get_task",
        _async_const(_row("failed", error_code="GUARDRAIL_VIOLATION", error_msg="blocked")),
    )
    _patch_steps(monkeypatch, [_step("guardrail_check_input", status="failed")])

    text = await _collect(client, f"/v1/tasks/{TASK_ID}/stream")
    events = [f["event"] for f in _parse_sse(text)]

    assert events[0] == "snapshot"
    assert events[-1] == "content_filter"  # guardrail block -> content_filter terminal


# ── failed (non-guardrail) terminal -> error frame ──────────────────────────────────
async def test_failed_task_emits_error_frame(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    monkeypatch.setattr(
        tasks_repo, "get_task",
        _async_const(_row("failed", error_code="SERVICE_UNAVAILABLE", error_msg="boom")),
    )
    _patch_steps(monkeypatch, [])

    text = await _collect(client, f"/v1/tasks/{TASK_ID}/stream")
    events = [f["event"] for f in _parse_sse(text)]

    assert events[-1] == "error"


# ── polling fallback (no Pub/Sub): snapshots until terminal ─────────────────────────
async def test_polling_fallback_runs_to_terminal(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    # The conftest _FakeValkey has NO subscribe_task_events -> the relay polls.
    assert not hasattr(app.state.valkey, "subscribe_task_events")
    settings = get_settings()
    monkeypatch.setattr(settings, "sse_poll_interval_seconds", 0.01, raising=False)

    # First poll: running; second poll: completed (so the stream closes).
    rows = [_row("running"), _row("completed", output={"message": "ok"})]
    calls = {"n": 0}

    async def _get_task(pool: Any, tenant_id: str, task_id: str) -> TaskRow:
        i = min(calls["n"], len(rows) - 1)
        calls["n"] += 1
        return rows[i]

    monkeypatch.setattr(tasks_repo, "get_task", _get_task)
    _patch_steps(monkeypatch, [_step()])

    text = await _collect(client, f"/v1/tasks/{TASK_ID}/stream")
    events = [f["event"] for f in _parse_sse(text)]

    # Initial snapshot, at least one polled snapshot, then the terminal done frame.
    assert events.count("snapshot") >= 1
    assert events[-1] == "done"


# ── configured Pub/Sub: live frames relayed; a done frame closes the stream ─────────
class _FakePubSubValkey:
    """A configured Valkey double exposing subscribe_task_events (live SSE relay path).

    Carries ``set_cancel_signal`` too: ``api.tasks._valkey_client`` only treats a Valkey as
    CONFIGURED (vs the conftest fake) when that helper is present — without it the stream
    endpoint would see ``valkey=None`` and take the polling fallback instead of Pub/Sub.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def set_cancel_signal(self, **_kw: Any) -> None:  # configured-Valkey marker
        return None

    async def subscribe_task_events(self, *, prefix: str, tenant_id: str, task_id: str
                                    ) -> AsyncIterator[dict[str, Any]]:
        import asyncio

        for ev in self._events:
            await asyncio.sleep(0)  # cooperative yield so the relay's wait_for resolves cleanly
            yield ev

    async def aclose(self) -> None:
        return None


async def test_pubsub_relay_relays_live_frames_then_done(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    monkeypatch.setattr(tasks_repo, "get_task", _async_const(_row("running")))
    _patch_steps(monkeypatch, [])
    settings = get_settings()
    # A comfortably-large poll timeout so the relay's wait_for resolves each live frame
    # (the generator yields immediately); a small max-duration bounds the worst case.
    monkeypatch.setattr(settings, "sse_poll_interval_seconds", 0.5, raising=False)
    monkeypatch.setattr(settings, "sse_max_duration_seconds", 5, raising=False)
    # Inject a configured Pub/Sub Valkey yielding a step frame then a terminal done frame.
    app.state.valkey = _FakePubSubValkey([
        {"event": "step", "task_id": TASK_ID, "step": "llm_call", "status": "passed"},
        {"event": "done", "task_id": TASK_ID, "status": "completed", "result": {"status": "completed"}},
    ])

    text = await _collect(client, f"/v1/tasks/{TASK_ID}/stream")
    frames = _parse_sse(text)
    events = [f["event"] for f in frames]

    assert "snapshot" in events  # the initial snapshot
    assert "step" in events  # a live relayed step frame
    assert events[-1] == "done"  # terminal frame closes the stream


# ── 404 for unknown / cross-tenant task (RLS hides it) ──────────────────────────────
async def test_unknown_task_is_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    monkeypatch.setattr(tasks_repo, "get_task", _async_const(None))  # RLS-hidden -> None

    resp = await client.get(f"/v1/tasks/{TASK_ID}/stream")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── streaming disabled by settings -> 404 ───────────────────────────────────────────
async def test_streaming_disabled_is_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    settings = get_settings()
    monkeypatch.setattr(settings, "sse_streaming_enabled", False, raising=False)

    resp = await client.get(f"/v1/tasks/{TASK_ID}/stream")
    assert resp.status_code == 404, resp.text


# ── helper: an async function returning a constant ───────────────────────────────────
def _async_const(value: Any) -> Any:
    async def _fn(*_a: Any, **_kw: Any) -> Any:
        return value

    return _fn
