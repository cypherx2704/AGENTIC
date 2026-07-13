"""Contract 3 — A2A task-response builder.

The PUBLIC ``POST /v1/tasks`` request is the loose body in ``models/task.py``; the
RESPONSE returned to the caller (and the A2A response shape) conforms to Contract 3
(contracts/a2a/task-response.schema.json).

BAKED-IN FIXES applied here:

* **FIX 2** — the internal ``xagent.task_steps.status`` enum includes ``redacted``,
  but the Contract 3 ``task_steps[].status`` enum is only
  ``passed | failed | timeout | skipped``. When BUILDING the response we map
  ``redacted -> passed``. The ``redacted`` value is preserved ONLY in the audit row
  (steps_repo), never in the wire response.
* **FIX 3** — the response ALWAYS includes ``schema_version="1.0.0"`` + ``started_at``
  + ``cost_usd`` + ``task_steps`` (the schema requires cost_usd + task_steps on
  completed responses; we include them unconditionally so smoke-test assertions and
  audit queries always have them).

The builder takes plain values (the api layer assembles them from the task row +
the pipeline context) and returns a JSON-ready ``dict`` — it does not touch the DB.
"""

from __future__ import annotations

from typing import Any

from .task import STEP_TYPE_TOOL_CALL

SCHEMA_VERSION = "1.0.0"

# Map internal task_steps.status -> Contract 3 task_steps[].status (FIX 2).
# 'running' should never reach the response (steps are finalised first); map it to
# 'skipped' defensively so an in-flight row can never violate the enum.
_STEP_STATUS_MAP: dict[str, str] = {
    "passed": "passed",
    "failed": "failed",
    "timeout": "timeout",
    "skipped": "skipped",
    "redacted": "passed",  # FIX 2 — A2A enum has no 'redacted'
    "running": "skipped",
}


def map_step_status(internal_status: str) -> str:
    """Map an internal step status to the Contract 3 enum (FIX 2: redacted -> passed)."""
    return _STEP_STATUS_MAP.get(internal_status, "passed")


#: Keys projected out of a ``tool_call`` audit step's ``output``. This is an ALLOW-LIST on purpose:
#: other step types put sensitive material in ``output`` (a guardrail step's ``violations`` can
#: carry the matched content), so the raw JSONB must never be forwarded wholesale.
_TOOL_OUTPUT_KEYS = ("tool", "tool_version", "tool_call_id", "error")


def build_step(
    *,
    step_name: str,
    status: str,
    duration_ms: int,
    tokens: int | None = None,
    step_type: str | None = None,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one Contract 3 ``task_steps[]`` entry from an internal audit step.

    ``status`` is the INTERNAL status (possibly ``redacted``); it is mapped here.

    ``step_type`` + the tool fields let a client tell WHICH tool a ``tool_call`` step invoked —
    the audit row records it, but every tool step's ``step_name`` is the literal ``"tool_call"``,
    so without this the tool's identity never reaches the wire. ``task_steps`` items are
    ``additionalProperties: true`` in the contract, so these extra keys are contract-safe.
    """
    step: dict[str, Any] = {
        "step": step_name,
        "status": map_step_status(status),
        "duration_ms": int(duration_ms),
    }
    if tokens is not None:
        step["tokens"] = int(tokens)
    if step_type is not None:
        step["step_type"] = step_type
    if step_type == STEP_TYPE_TOOL_CALL and isinstance(output, dict):
        for key in _TOOL_OUTPUT_KEYS:
            value = output.get(key)
            if value is not None:
                step[key] = value
    return step


def build_task_response(
    *,
    task_id: str,
    status: str,
    trace_id: str,
    started_at: str,
    task_steps: list[dict[str, Any]],
    completed_at: str | None = None,
    duration_ms: int | None = None,
    tokens_used: int | None = None,
    cost_usd: float = 0.0,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a Contract 3 task-response dict (FIX 3 fields always present).

    Args:
        task_id:      UUID of the task (echoes the created task row).
        status:       task status — pending | running (honest non-terminal GET
                      projection, amended plan) | completed | failed | cancelled |
                      timeout.
        trace_id:     UUID distributed-trace id (Contract 8).
        started_at:   RFC 3339 UTC ms-precision execution-start timestamp.
        task_steps:   list of entries already mapped via ``build_step`` (FIX 2 applied).
        completed_at: RFC 3339 UTC ms-precision finish timestamp (when known).
        duration_ms:  total wall-clock ms.
        tokens_used:  total tokens across steps.
        cost_usd:     total USD cost (REQUIRED on completed; always emitted — FIX 3).
        output:       task-type-specific output (e.g. ``{"message": "..."}``) on success.
        error:        Contract 2 error-shape dict on failed/cancelled/timeout.
        metadata:     the caller's free-form task tags (persisted ``tasks.metadata``);
                      emitted when provided (the GET projection passes it).

    Returns:
        A JSON-ready dict conforming to a2a/task-response.schema.json.
    """
    response: dict[str, Any] = {
        "task_id": task_id,
        "schema_version": SCHEMA_VERSION,  # FIX 3 — always present
        "status": status,
        "started_at": started_at,  # FIX 3 — always present
        "trace_id": trace_id,
        # FIX 3 — cost_usd + task_steps always present (required on completed).
        "cost_usd": float(cost_usd),
        "task_steps": task_steps,
    }
    if completed_at is not None:
        response["completed_at"] = completed_at
    if duration_ms is not None:
        response["duration_ms"] = int(duration_ms)
    if tokens_used is not None:
        response["tokens_used"] = int(tokens_used)
    if output is not None:
        response["output"] = output
    if metadata is not None:
        response["metadata"] = metadata
    response["error"] = error  # null on success; Contract 2 shape on failure
    return response
