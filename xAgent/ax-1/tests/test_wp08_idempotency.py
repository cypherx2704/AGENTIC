"""WP08 — Contract-9 Idempotency-Key reservation/replay on POST /v1/tasks.

The endpoint composes ``_idempotency_begin`` (a CONFIGURED Valkey only — the conftest
fake lacks the helpers so idempotency is DISABLED there). To exercise the ENABLED
behaviours we inject an in-memory fake that implements ``idempotency_reserve`` /
``idempotency_complete`` / ``idempotency_release`` (the methods ``api.tasks`` calls):

  * first POST with a key      -> new reservation -> proceeds -> 200 + response stored;
  * duplicate while in_flight   -> 409 CONFLICT;
  * duplicate after completed   -> replay of the stored response (Idempotent-Replayed: true);
  * CONFIGURED Valkey that errors on reserve -> 503 FAIL-CLOSED;
  * no Idempotency-Key header   -> disabled (proceeds, no reservation touched).

To keep the run deterministic with no DB / LLM, we monkeypatch the task-create/run seam:
``tasks_repo.create_task`` + ``tasks_repo.mark_running`` are stubbed and the pipeline is
replaced with a trivial double that finalises ``completed`` so the response is built and
``finish_idem`` stores it. Authorize is disabled via a settings override so only the
idempotency layer is under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.api import tasks as tasks_api
from agent_runtime.core.config import get_settings
from agent_runtime.db import tasks_repo
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.services.valkey import IdempotencyRecord

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"


# ── In-memory fake Valkey implementing the WP08 idempotency interface ───────────────
class FakeIdemValkey:
    """Configured-Valkey double for the Contract-9 idempotency store.

    Mirrors ``ValkeyClient`` semantics: ``idempotency_reserve`` does a SET-NX —
    first caller gets None (won), later callers get the stored record. The presence of
    ``set_cancel_signal`` is what makes ``_valkey_client`` treat this as CONFIGURED; we
    add a no-op one (cancel is not under test here) plus the idempotency helpers.
    """

    def __init__(self, *, raise_on_reserve: Exception | None = None) -> None:
        self.records: dict[str, IdempotencyRecord] = {}
        self.raise_on_reserve = raise_on_reserve
        self.completed: list[tuple[str, int]] = []
        self.released: list[str] = []

    @staticmethod
    def _key(prefix: str, tenant_id: str, key: str) -> str:
        return f"{prefix}idem:{tenant_id}:{key}"

    async def idempotency_reserve(
        self,
        *,
        prefix: str,
        tenant_id: str,
        key: str,
        ttl_seconds: int,
        timeout_seconds: float,
        fingerprint: str | None = None,
    ) -> IdempotencyRecord | None:
        if self.raise_on_reserve is not None:
            raise self.raise_on_reserve
        rkey = self._key(prefix, tenant_id, key)
        existing = self.records.get(rkey)
        if existing is not None:
            return existing
        self.records[rkey] = IdempotencyRecord(state="in_flight", fingerprint=fingerprint)  # SET-NX won
        return None

    async def idempotency_complete(
        self,
        *,
        prefix: str,
        tenant_id: str,
        key: str,
        status_code: int,
        response: dict[str, Any],
        ttl_seconds: int,
        timeout_seconds: float,
        fingerprint: str | None = None,
    ) -> None:
        rkey = self._key(prefix, tenant_id, key)
        self.records[rkey] = IdempotencyRecord(
            state="completed", status_code=status_code, response=response, fingerprint=fingerprint
        )
        self.completed.append((key, status_code))

    async def idempotency_release(
        self, *, prefix: str, tenant_id: str, key: str, timeout_seconds: float
    ) -> None:
        rkey = self._key(prefix, tenant_id, key)
        self.records.pop(rkey, None)
        self.released.append(key)

    # Marker method so api.tasks._valkey_client treats this as a CONFIGURED client.
    async def set_cancel_signal(self, **_kwargs: Any) -> None:
        return None

    async def clear_cancel_signal(self, **_kwargs: Any) -> None:
        return None

    async def is_cancelled(self, **_kwargs: Any) -> bool:
        return False

    async def aclose(self) -> None:
        return None


class _CompletedPipeline:
    """Pipeline double: leaves ctx with no terminal_error so the response is 'completed'."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    @classmethod
    def from_registry(cls, _event_stage: Any) -> _CompletedPipeline:
        return cls()

    async def run(self, ctx: Any) -> Any:
        ctx.final_answer = "done"
        return ctx


def _install_no_db_seam(monkeypatch: Any, app: Any, *, authorize: bool = False) -> None:
    """Stub the create/run seam so POST /v1/tasks runs with no DB / LLM / authorize.

    The pipeline is replaced with a trivial 'completed' double, the two task-repo writes
    are no-ops, and authorize is disabled by default so only idempotency is under test.
    """
    app.state.db_pool = object()

    async def _fake_create(pool: Any, **kw: Any) -> TaskRow:
        return TaskRow(
            task_id=TASK_ID,
            agent_id=TEST_AGENT,
            tenant_id=TEST_TENANT,
            trace_id=kw.get("trace_id") or "trace",
            status="pending",
            input=kw.get("task_input") or {"message": "hi"},
        )

    async def _fake_mark_running(pool: Any, tenant_id: str, task_id: str) -> None:
        return None

    monkeypatch.setattr(tasks_repo, "create_task", _fake_create)
    monkeypatch.setattr(tasks_repo, "mark_running", _fake_mark_running)
    monkeypatch.setattr(tasks_api, "Pipeline", _CompletedPipeline)

    settings = get_settings().model_copy(update={"authorize_enabled": authorize})
    monkeypatch.setattr(tasks_api, "get_settings", lambda: settings)


def _post_body() -> dict[str, Any]:
    return {"agent_id": TEST_AGENT, "input": {"message": "What is 2 + 2?"}, "mode": "sync"}


# ── new reservation -> 200 + response stored for replay ─────────────────────────────
async def test_first_request_with_key_succeeds_and_stores_response(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    fake_vk = FakeIdemValkey()
    app.state.valkey = fake_vk
    _install_no_db_seam(monkeypatch, app)

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-1"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    # The terminal response was stored against the key for future replay.
    assert fake_vk.completed == [("k-1", 200)]


# ── duplicate while in_flight -> 409 ────────────────────────────────────────────────
async def test_duplicate_in_flight_returns_409(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    fake_vk = FakeIdemValkey()
    app.state.valkey = fake_vk
    _install_no_db_seam(monkeypatch, app)

    # Pre-seed an in_flight reservation for k-2 (an original request still executing).
    s = get_settings()
    rkey = FakeIdemValkey._key(s.task_signal_key_prefix, TEST_TENANT, "k-2")
    fake_vk.records[rkey] = IdempotencyRecord(state="in_flight")

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-2"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


# ── duplicate after completed -> replay the stored response ─────────────────────────
async def test_duplicate_after_completed_replays(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    fake_vk = FakeIdemValkey()
    app.state.valkey = fake_vk
    _install_no_db_seam(monkeypatch, app)

    stored = {"task_id": TASK_ID, "status": "completed", "output": {"message": "cached"}}
    s = get_settings()
    rkey = FakeIdemValkey._key(s.task_signal_key_prefix, TEST_TENANT, "k-3")
    fake_vk.records[rkey] = IdempotencyRecord(state="completed", status_code=200, response=stored)

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-3"})

    assert resp.status_code == 200, resp.text
    assert resp.headers.get("Idempotent-Replayed") == "true"
    assert resp.json() == stored  # the STORED response, not a fresh run
    # A replay must not re-store anything (no new completion for k-3).
    assert fake_vk.completed == []


# ── same key + DIFFERENT body -> 409 IDEMPOTENCY_KEY_CONFLICT (Contract-15 case 13) ──
async def test_duplicate_with_different_body_conflicts(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    fake_vk = FakeIdemValkey()
    app.state.valkey = fake_vk
    _install_no_db_seam(monkeypatch, app)

    # Pre-seed a completed record whose fingerprint will NOT match the incoming body's.
    stored = {"task_id": TASK_ID, "status": "completed"}
    s = get_settings()
    rkey = FakeIdemValkey._key(s.task_signal_key_prefix, TEST_TENANT, "k-conflict")
    fake_vk.records[rkey] = IdempotencyRecord(
        state="completed", status_code=200, response=stored, fingerprint="some-other-body-hash"
    )

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-conflict"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert resp.json()["error"]["details"]["reason"] == "IDEMPOTENCY_KEY_CONFLICT"


# ── CONFIGURED Valkey errors on reserve -> 503 FAIL-CLOSED ──────────────────────────
async def test_reserve_error_fails_closed_503(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.valkey = FakeIdemValkey(raise_on_reserve=RuntimeError("valkey unavailable"))
    _install_no_db_seam(monkeypatch, app)

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-4"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── no Idempotency-Key header -> disabled, proceeds normally ─────────────────────────
async def test_no_key_disables_idempotency(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    fake_vk = FakeIdemValkey()
    app.state.valkey = fake_vk
    _install_no_db_seam(monkeypatch, app)

    resp = await client.post("/v1/tasks", json=_post_body())  # no header

    assert resp.status_code == 200, resp.text
    # Idempotency never reserved or completed anything without a key.
    assert fake_vk.records == {}
    assert fake_vk.completed == []


# ── no CONFIGURED Valkey (conftest fake) + a key -> disabled/allow ──────────────────
async def test_no_configured_valkey_allows(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    # Keep the conftest network-free fake (no idempotency helpers) -> feature disabled.
    assert not hasattr(app.state.valkey, "idempotency_reserve")
    _install_no_db_seam(monkeypatch, app)

    resp = await client.post("/v1/tasks", json=_post_body(), headers={"Idempotency-Key": "k-5"})

    assert resp.status_code == 200, resp.text


# ── unit-level: _idempotency_begin replay path returns the stored status_code ───────
@pytest.mark.asyncio
async def test_idempotency_begin_replay_preserves_status_code(monkeypatch: Any) -> None:
    from agent_runtime.core.auth import Principal

    fake_vk = FakeIdemValkey()
    s = get_settings()
    rkey = FakeIdemValkey._key(s.task_signal_key_prefix, TEST_TENANT, "k-9")
    fake_vk.records[rkey] = IdempotencyRecord(
        state="completed", status_code=200, response={"status": "completed"}
    )
    principal = Principal(tenant_id=TEST_TENANT, agent_id=TEST_AGENT, scopes=["agent:execute"], raw_token="t")

    replay, finish = await tasks_api._idempotency_begin(principal, s, fake_vk, "k-9")

    assert replay is not None
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replayed") == "true"
