"""PUBLIC task-submission request model (Component 2).

This is the body of ``POST /v1/tasks`` — the public, client-facing request. It is
DELIBERATELY NOT the strict a2a/task-request contract schema (BAKED-IN FIX 4): the
public body is ``{agent_id, input:{message}, mode, priority?, timeout_seconds?, metadata?}``
and only the *response* conforms to Contract 3.

First-cycle validation rules (each violation -> 422 VALIDATION_ERROR):
  * ``mode`` must be ``sync`` (async/stream are 📋).
  * ``input`` serialised JSON must not exceed 256 KiB (Contract 3 A2A parity).
  * ``timeout_seconds`` must be in [1, 900].
  * the body MUST NOT carry identity/correlation fields (tenant_id, trace_id, ...) —
    identity comes from the JWT only (Contract 13).

Identity is never read from this body: ``agent_id`` here is the *target* agent to run
(it is validated against the JWT's tenant via the agents repo / runtime config), not a
spoofable identity claim.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.errors import ApiError, ErrorCode

# 256 KiB serialised-JSON cap on ``input`` (Contract 3 A2A parity).
MAX_INPUT_BYTES = 256 * 1024

# ── Canonical task_steps.step_type vocabulary (Component 6 + WP12 enhancement stages) ──
# The single source of truth for the step_type values the pipeline writes, kept here so
# every stage references the SAME constant (and the DB CHECK constraint mirrors this set —
# see migration 20260611_0005). The first three are the first-cycle (Phase 9A) types; the
# rest are the WP12 enhancement-stage types (RAG / Memory / Tools / budget truncation).
STEP_TYPE_GUARDRAIL_CHECK = "guardrail_check"
STEP_TYPE_LLM_CALL = "llm_call"
STEP_TYPE_SKILL_LOAD = "skill_load"
STEP_TYPE_RAG_QUERY = "rag_query"
STEP_TYPE_MEMORY_RETRIEVE = "memory_retrieve"
STEP_TYPE_MEMORY_WRITE = "memory_write"
STEP_TYPE_TOOL_CALL = "tool_call"
STEP_TYPE_TOOL_LOOP_LIMIT = "tool_loop_limit"
STEP_TYPE_CONTEXT_TRUNCATED = "context_truncated"

# The full set (mirrors the task_steps.step_type CHECK constraint after WP12).
STEP_TYPES = frozenset(
    {
        STEP_TYPE_GUARDRAIL_CHECK,
        STEP_TYPE_LLM_CALL,
        STEP_TYPE_SKILL_LOAD,
        STEP_TYPE_RAG_QUERY,
        STEP_TYPE_MEMORY_RETRIEVE,
        STEP_TYPE_MEMORY_WRITE,
        STEP_TYPE_TOOL_CALL,
        STEP_TYPE_TOOL_LOOP_LIMIT,
        STEP_TYPE_CONTEXT_TRUNCATED,
    }
)

# Identity / correlation keys that MUST NOT appear in the body or in metadata
# (Contract 13 anti-spoof guard). tenant_id / agent identity flow via the JWT only.
RESERVED_BODY_FIELDS = frozenset(
    {"tenant_id", "trace_id", "span_id", "request_id", "task_id", "user_id", "org_id"}
)
RESERVED_METADATA_KEYS = RESERVED_BODY_FIELDS | {"agent_id"}


class TaskInput(BaseModel):
    """The ``input`` envelope. First cycle carries a single ``message`` string."""

    model_config = ConfigDict(extra="allow")

    message: str = Field(..., min_length=1)


class TaskRequest(BaseModel):
    """Body of ``POST /v1/tasks`` (public client request)."""

    # extra='forbid' so unknown / reserved identity keys at the top level are rejected
    # as 422 VALIDATION_ERROR; metadata is checked separately for reserved keys.
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., description="Target agent to execute (validated vs JWT tenant).")
    input: TaskInput
    mode: Literal["sync"] = Field(
        default="sync",
        description="First cycle accepts ONLY 'sync'; async/stream -> 422.",
    )
    priority: Literal["low", "normal", "high"] = "normal"
    timeout_seconds: int = Field(default=120, description="Clamped to [1, 900].")
    # Optional conversational-session correlator (WP12). NOT an identity claim — it scopes
    # session-scoped memory retrieve/write to one conversation; tenant/agent still come
    # from the JWT only. Persisted to xagent.tasks.session_id and read by the memory stages.
    session_id: str | None = Field(
        default=None,
        max_length=255,
        description="Optional session correlator for session-scoped memory.",
    )
    # Optional per-task COST budget in USD (WP12). When set, the LLM + tool stages accrue
    # cost against it and short-circuit BUDGET_EXCEEDED before exceeding it. None means no
    # cost cap (the token_budget_per_task still bounds the single LLM call). Persisted to
    # xagent.tasks.cost_budget_per_task.
    cost_budget_per_task: float | None = Field(
        default=None,
        gt=0.0,
        description="Optional per-task USD cost budget; None disables the cost cap.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mode", mode="before")
    @classmethod
    def _reject_non_sync(cls, v: Any) -> Any:
        if v is not None and v != "sync":
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Only mode='sync' is supported in the first cycle.",
                details={"reason": "MODE_NOT_SUPPORTED", "mode": v},
            )
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if not (1 <= v <= 900):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "timeout_seconds must be in [1, 900].",
                details={"reason": "TIMEOUT_OUT_OF_RANGE", "timeout_seconds": v},
            )
        return v

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        reserved = RESERVED_METADATA_KEYS.intersection(v.keys())
        if reserved:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "metadata must not contain reserved identity keys.",
                details={"reason": "RESERVED_METADATA_KEY", "keys": sorted(reserved)},
            )
        return v

    @model_validator(mode="after")
    def _validate_input_size(self) -> TaskRequest:
        serialised = json.dumps(self.input.model_dump(), separators=(",", ":"))
        if len(serialised.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "input exceeds the 256 KiB size cap.",
                details={"reason": "INPUT_TOO_LARGE", "max_bytes": MAX_INPUT_BYTES},
            )
        return self
