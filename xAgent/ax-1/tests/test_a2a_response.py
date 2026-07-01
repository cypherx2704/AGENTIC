"""Unit tests for the Contract 3 task-response builder (``models/a2a.py``).

The builder is a pure function (no DB / network). These tests pin the two baked-in
fixes the area depends on:

  * **FIX 3** — the response ALWAYS includes ``schema_version="1.0.0"`` + ``started_at``
    + ``cost_usd`` + ``task_steps`` (required on completed responses; emitted always).
  * **FIX 2** — an internal step status of ``redacted`` is mapped to ``passed`` in the
    wire response (the A2A ``task_steps[].status`` enum is passed|failed|timeout|skipped;
    it has no ``redacted``). ``redacted`` is preserved only in the audit row.

Step-name fidelity is also checked: a first-cycle task projects EXACTLY the three
ordered steps guardrail_check_input -> llm_call -> guardrail_check_output.
"""

from __future__ import annotations

from agent_runtime.models import a2a

TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"
STARTED_AT = "2026-06-08T12:00:00.000Z"
COMPLETED_AT = "2026-06-08T12:00:01.250Z"


# ── FIX 2: map_step_status / build_step ───────────────────────────────────────────
def test_map_step_status_redacted_to_passed() -> None:
    assert a2a.map_step_status("redacted") == "passed"


def test_map_step_status_passthrough_for_a2a_enum() -> None:
    for status in ("passed", "failed", "timeout", "skipped"):
        assert a2a.map_step_status(status) == status


def test_map_step_status_running_defensively_skipped() -> None:
    # 'running' must never reach the wire enum; mapped defensively to 'skipped'.
    assert a2a.map_step_status("running") == "skipped"


def test_build_step_maps_redacted_and_keeps_fields() -> None:
    step = a2a.build_step(step_name="guardrail_check_output", status="redacted", duration_ms=12)
    assert step["step"] == "guardrail_check_output"
    assert step["status"] == "passed"  # FIX 2 — redacted -> passed on the wire
    assert step["duration_ms"] == 12
    # tokens omitted when None.
    assert "tokens" not in step


def test_build_step_includes_tokens_when_present() -> None:
    step = a2a.build_step(step_name="llm_call", status="passed", duration_ms=340, tokens=42)
    assert step["tokens"] == 42


# ── FIX 3: required fields always present ──────────────────────────────────────────
def test_completed_response_has_all_required_fields() -> None:
    steps = [
        a2a.build_step(step_name="guardrail_check_input", status="passed", duration_ms=5),
        a2a.build_step(step_name="llm_call", status="passed", duration_ms=300, tokens=80),
        a2a.build_step(step_name="guardrail_check_output", status="redacted", duration_ms=7),
    ]
    resp = a2a.build_task_response(
        task_id=TASK_ID,
        status="completed",
        trace_id=TRACE_ID,
        started_at=STARTED_AT,
        task_steps=steps,
        completed_at=COMPLETED_AT,
        duration_ms=312,
        tokens_used=80,
        cost_usd=0.0021,
        output={"message": "Hello!"},
    )

    # FIX 3 — these four are unconditionally present.
    assert resp["schema_version"] == "1.0.0"
    assert resp["started_at"] == STARTED_AT
    assert resp["cost_usd"] == 0.0021
    assert resp["task_steps"] == steps

    assert resp["task_id"] == TASK_ID
    assert resp["status"] == "completed"
    assert resp["trace_id"] == TRACE_ID
    assert resp["completed_at"] == COMPLETED_AT
    assert resp["duration_ms"] == 312
    assert resp["tokens_used"] == 80
    assert resp["output"] == {"message": "Hello!"}
    # error key present and null on success.
    assert resp["error"] is None


def test_completed_step_names_and_order_first_cycle() -> None:
    steps = [
        a2a.build_step(step_name="guardrail_check_input", status="passed", duration_ms=5),
        a2a.build_step(step_name="llm_call", status="passed", duration_ms=300, tokens=80),
        a2a.build_step(step_name="guardrail_check_output", status="redacted", duration_ms=7),
    ]
    resp = a2a.build_task_response(
        task_id=TASK_ID,
        status="completed",
        trace_id=TRACE_ID,
        started_at=STARTED_AT,
        task_steps=steps,
        cost_usd=0.0,
    )
    names = [s["step"] for s in resp["task_steps"]]
    assert names == ["guardrail_check_input", "llm_call", "guardrail_check_output"]
    # The redacted POST-guardrail step shows as 'passed' on the wire (FIX 2).
    statuses = [s["status"] for s in resp["task_steps"]]
    assert statuses == ["passed", "passed", "passed"]


def test_cost_usd_always_present_even_when_zero() -> None:
    resp = a2a.build_task_response(
        task_id=TASK_ID,
        status="completed",
        trace_id=TRACE_ID,
        started_at=STARTED_AT,
        task_steps=[],
        # cost_usd defaulted -> 0.0; still emitted (FIX 3).
    )
    assert "cost_usd" in resp
    assert resp["cost_usd"] == 0.0
    assert isinstance(resp["cost_usd"], float)


def test_failed_response_carries_contract2_error_shape() -> None:
    error = {
        "code": "GUARDRAIL_VIOLATION",
        "message": "Prompt injection detected.",
        "request_id": "req-1",
        "trace_id": TRACE_ID,
        "timestamp": COMPLETED_AT,
    }
    resp = a2a.build_task_response(
        task_id=TASK_ID,
        status="failed",
        trace_id=TRACE_ID,
        started_at=STARTED_AT,
        task_steps=[
            a2a.build_step(step_name="guardrail_check_input", status="failed", duration_ms=4),
        ],
        completed_at=COMPLETED_AT,
        error=error,
    )
    assert resp["status"] == "failed"
    assert resp["error"] == error
    # FIX 3 invariants still hold on the failure path.
    assert resp["schema_version"] == "1.0.0"
    assert resp["started_at"] == STARTED_AT
    assert "cost_usd" in resp
    # No output on failure.
    assert "output" not in resp
