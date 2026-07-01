"""WP02 — honest GET /v1/tasks/{id} projection + tasks.metadata persistence.

Honest projection (amended plan): a NON-TERMINAL task reports its REAL current status
(``pending`` / ``running``) with the audit steps written so far. The old
``_A2A_STATUSES`` fallback coerced ``running`` to a fake ``failed``; the set now carries
the full Contract-3 status list including the non-terminal values.

tasks.metadata (amended codification): ``TaskRequest.metadata`` (reserved keys already
rejected at validation) persists to the new ``xagent.tasks.metadata`` JSONB column and
is included in the GET response. The DB round-trip test runs against the local task
store when reachable (the migration is applied there) and skips cleanly otherwise.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.api.tasks import _A2A_STATUSES, _response_from_task_row
from agent_runtime.core.config import get_settings
from agent_runtime.db import steps_repo, tasks_repo
from agent_runtime.db.steps_repo import StepRow
from agent_runtime.db.tasks_repo import TaskRow

# Mirrors the conftest fixed Principal (tests/ is not an importable package).
TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


def _row(status: str, **overrides: Any) -> TaskRow:
    row = TaskRow(
        task_id=TASK_ID,
        agent_id=TEST_AGENT,
        tenant_id=TEST_TENANT,
        trace_id=TRACE_ID,
        status=status,
        input={"message": "hi"},
        metadata={"campaign": "q2-launch"},
        created_at="2026-06-10T12:00:00.000Z",
        started_at="2026-06-10T12:00:00.100Z" if status != "pending" else None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _running_steps() -> list[StepRow]:
    return [
        StepRow(
            task_id=TASK_ID,
            tenant_id=TEST_TENANT,
            step_type="guardrail_check",
            step_name="guardrail_check_input",
            status="passed",
            duration_ms=3,
        )
    ]


# ── Honest non-terminal projection (the old fallback coerced running -> failed) ──────
def test_running_task_reports_running_not_failed() -> None:
    response = _response_from_task_row(_row("running"), _running_steps())
    assert response["status"] == "running"  # NOT coerced to 'failed'
    assert response["error"] is None
    # The steps so far are projected.
    assert [s["step"] for s in response["task_steps"]] == ["guardrail_check_input"]
    # No fake terminal fields.
    assert "completed_at" not in response


def test_pending_task_reports_pending() -> None:
    response = _response_from_task_row(_row("pending"), [])
    assert response["status"] == "pending"
    assert response["task_steps"] == []
    assert response["error"] is None


def test_a2a_statuses_cover_full_contract3_set() -> None:
    expected = {"pending", "running", "completed", "failed", "cancelled", "timeout"}
    assert set(_A2A_STATUSES) == expected


# ── metadata included in the GET projection ──────────────────────────────────────────
def test_metadata_included_in_get_response() -> None:
    response = _response_from_task_row(_row("running"), [])
    assert response["metadata"] == {"campaign": "q2-launch"}


# ── HTTP-level GET: real endpoint, repos monkeypatched (no DB needed) ────────────────
async def test_http_get_running_task_is_honest(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    row = _row("running")
    steps = _running_steps()

    async def _fake_get_task(pool: Any, tenant_id: str, task_id: str) -> TaskRow:
        assert tenant_id == TEST_TENANT
        assert task_id == TASK_ID
        return row

    async def _fake_list_steps(pool: Any, tenant_id: str, task_id: str) -> list[StepRow]:
        return steps

    monkeypatch.setattr(tasks_repo, "get_task", _fake_get_task)
    monkeypatch.setattr(steps_repo, "list_steps", _fake_list_steps)
    client._transport.app.state.db_pool = object()  # repos are patched; pool is a handle only

    resp = await client.get(f"/v1/tasks/{TASK_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["error"] is None
    assert [s["step"] for s in body["task_steps"]] == ["guardrail_check_input"]
    assert body["metadata"] == {"campaign": "q2-launch"}


# ── DB round-trip: metadata persists to the new JSONB column (skip if no DB) ────────
async def test_metadata_persists_to_db_roundtrip() -> None:
    from agent_runtime.db import pool as db_pool

    settings = get_settings()
    test_pool = db_pool.create_pool(settings.database_url)
    try:
        await test_pool.open(wait=True, timeout=3.0)
    except Exception:  # noqa: BLE001 — no DB available in this environment
        await test_pool.close()
        pytest.skip("No task store (Postgres) reachable for the metadata round-trip.")

    try:
        created = await tasks_repo.create_task(
            test_pool,
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            trace_id=TRACE_ID,
            task_input={"message": "hi"},
            timeout_seconds=120,
            metadata={"campaign": "q2-launch", "run": 7},
        )
        assert created.metadata == {"campaign": "q2-launch", "run": 7}
        assert created.status == "pending"  # honest pre-execution status

        fetched = await tasks_repo.get_task(test_pool, TEST_TENANT, created.task_id)
        assert fetched is not None
        assert fetched.metadata == {"campaign": "q2-launch", "run": 7}

        # And the GET projection carries it (honest status + metadata).
        response = _response_from_task_row(fetched, [])
        assert response["status"] == "pending"
        assert response["metadata"] == {"campaign": "q2-launch", "run": 7}
    finally:
        await test_pool.close()
