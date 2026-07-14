"""The execution tree's per-node steps: what a sub-agent DID, projected safely onto the wire.

The run tree shows each sub-agent's audit trail so its TOOL CALLS are visible. Those steps come off
the sub-agent's own task, and their raw ``output`` JSONB is NOT safe to forward wholesale — a
guardrail step's output can carry the matched (i.e. offending) content. The projection is therefore
an ALLOW-LIST, shared with the single-agent task API. These tests pin that.
"""

from __future__ import annotations

from agent_runtime.api.orchestrations import _node_dict, _node_steps
from agent_runtime.orchestration.repo import NodeStepRow, WorkflowTaskRow

TASK = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _step(step_type: str, step_name: str, **kw: object) -> NodeStepRow:
    return NodeStepRow(
        task_id=TASK,
        step_type=step_type,
        step_name=step_name,
        status=str(kw.pop("status", "passed")),
        duration_ms=int(kw.pop("duration_ms", 12)),  # type: ignore[arg-type]
        tokens_used=kw.pop("tokens_used", None),  # type: ignore[arg-type]
        output=kw.pop("output", None),  # type: ignore[arg-type]
    )


def _node(task_id: str | None) -> WorkflowTaskRow:
    return WorkflowTaskRow(
        id="pk", workflow_id="wf", tenant_id="t", node_id="research", node_type="agent",
        status="completed", version=1, task_id=task_id, preset="gh-researcher", depends_on=[],
    )


# ── the tool call is the whole point: it must reach the wire ─────────────────────────────
def test_tool_call_step_exposes_which_tool_ran() -> None:
    """Every tool step's step_name is the literal 'tool_call' — without the projected `tool` field the
    tool's IDENTITY never reaches the UI, which is exactly the bug this feature exists to fix."""
    call = _step("tool_call", "tool_call", output={"tool": "tool-github-stats-abc", "tool_call_id": "c1"})
    steps = _node_steps(TASK, {TASK: [call]})
    assert len(steps) == 1
    assert steps[0]["step_type"] == "tool_call"
    assert steps[0]["tool"] == "tool-github-stats-abc"
    assert steps[0]["tool_call_id"] == "c1"


def test_failed_tool_call_carries_its_error() -> None:
    failed = _step("tool_call", "tool_call", status="failed", output={"tool": "t", "error": "TOOL_DENIED"})
    steps = _node_steps(TASK, {TASK: [failed]})
    assert steps[0]["error"] == "TOOL_DENIED"
    assert steps[0]["status"] == "failed"


def test_full_pipeline_trail_is_returned_in_order() -> None:
    """The node shows its whole run (guardrail -> llm -> tool -> guardrail), like the Task Runner."""
    steps = _node_steps(
        TASK,
        {
            TASK: [
                _step("guardrail_check", "guardrail_check_input"),
                _step("llm_call", "llm_call", tokens_used=120),
                _step("tool_call", "tool_call", output={"tool": "wiki"}),
                _step("guardrail_check", "guardrail_check_output"),
            ]
        },
    )
    assert [s["step"] for s in steps] == [
        "guardrail_check_input", "llm_call", "tool_call", "guardrail_check_output",
    ]
    assert steps[1]["tokens"] == 120


# ── the allow-list: everything else in `output` stays server-side ────────────────────────
def test_a_guardrail_steps_output_is_never_leaked() -> None:
    """A guardrail step's output can hold the MATCHED CONTENT (the very thing that was blocked).
    Forwarding raw `output` would publish it into the run tree. Only tool_call steps project, and
    only their four allow-listed keys."""
    secret = {"violations": [{"rule": "pii", "matched_text": "4111 1111 1111 1111"}]}
    steps = _node_steps(TASK, {TASK: [_step("guardrail_check", "guardrail_check_input", output=secret)]})

    assert len(steps) == 1
    blob = repr(steps[0])
    assert "4111" not in blob
    assert "violations" not in steps[0]
    assert "matched_text" not in blob
    # It still reports THAT the step ran, and how it went — just not what was in it.
    assert steps[0]["step"] == "guardrail_check_input"
    assert steps[0]["status"] == "passed"


def test_non_tool_steps_never_gain_tool_keys() -> None:
    steps = _node_steps(TASK, {TASK: [_step("llm_call", "llm_call", output={"tool": "not-a-tool-step"})]})
    assert "tool" not in steps[0]


def test_redacted_step_status_maps_to_passed_on_the_wire() -> None:
    """`redacted` is an internal-only status (Contract 3 has no such value) — same mapping the task
    API applies, because this reuses the same builder."""
    steps = _node_steps(TASK, {TASK: [_step("guardrail_check", "guardrail_check_output", status="redacted")]})
    assert steps[0]["status"] == "passed"


# ── node wiring ──────────────────────────────────────────────────────────────────────────
def test_node_dict_carries_its_own_steps() -> None:
    node = _node_dict(
        _node(TASK), {TASK: [_step("tool_call", "tool_call", output={"tool": "wiki"})]}
    )
    assert node["node_id"] == "research"
    assert [s["tool"] for s in node["steps"]] == ["wiki"]


def test_a_node_with_no_task_yet_has_no_steps() -> None:
    """A pending node has not created its task, so it has nothing to show — and must not blow up."""
    assert _node_dict(_node(None), {})["steps"] == []
    assert _node_steps(None, {}) == []


def test_a_node_whose_task_has_not_written_steps_yet_is_empty_not_missing() -> None:
    """A node that has JUST started (task row exists, no audit rows yet) renders an empty trail —
    this is the live case, and it must be an empty list, never a KeyError."""
    assert _node_dict(_node(TASK), {})["steps"] == []
