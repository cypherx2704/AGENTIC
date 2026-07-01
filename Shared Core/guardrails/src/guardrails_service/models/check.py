"""Request / response models for ``POST /v1/check/input`` and ``/v1/check/output``.

Identity comes from the JWT and trace context ONLY (Contract 13). The request body
MUST NOT carry identity fields — if ``agent_id``, ``tenant_id``, ``trace_id``,
``span_id``, ``request_id``, or ``check_id`` appear in the body the service returns
400 VALIDATION_ERROR (handled in the API layer so the message is explicit and the
status is 400, not pydantic's default 422).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Body fields that would let a caller spoof identity / correlation (Contract 13).
RESERVED_BODY_FIELDS: frozenset[str] = frozenset(
    {"agent_id", "tenant_id", "trace_id", "span_id", "request_id", "check_id"}
)

Decision = Literal["allow", "warn", "redact", "block"]
Action = Literal["allow", "warn", "redact", "block"]
Severity = Literal["info", "low", "medium", "high", "critical"]


class CheckRequest(BaseModel):
    """Body of ``POST /v1/check/input`` and ``POST /v1/check/output``.

    ``input_text`` is only meaningful for the output check (``output-pii-email-v1``
    distinguishes "email NOT in the input" from "user echoed their own email"). It is
    ignored on the input check.
    """

    # extra='forbid' so unknown keys (incl. reserved identity fields) are rejected;
    # the API layer first inspects the raw body to emit the precise reserved-field error.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., description="The text to check (prompt for input, response for output).")
    input_text: str | None = Field(
        default=None,
        description="Original user input (output check only); enables 'email not in input' logic.",
    )
    task_id: str | None = Field(default=None, description="Correlation only; NOT identity.")
    policy_set_id: str | None = Field(
        default=None, description="null = resolve via agent/tenant/platform chain."
    )
    mode: Literal["sync", "async"] = Field(default="sync", description="sync only in first cycle.")
    # ── ADDITIVE optional fields (default None => behaviour unchanged) ───────────────
    untrusted_spans: list[str] | None = Field(
        default=None,
        description=(
            "RAG/tool-provided spans within `text` to SPOTLIGHT (instruction-hierarchy). An "
            "injection/jailbreak pattern found inside one of these is treated as higher-risk."
        ),
    )
    grounding: list[str] | None = Field(
        default=None,
        description=(
            "Output check only: context passages the response should be grounded in (in "
            "addition to `input_text`). Feeds the optional groundedness/hallucination signal."
        ),
    )


class Violation(BaseModel):
    """A single rule that fired during a check."""

    rule_id: str
    rule_name: str
    severity: Severity
    category: str
    matched: str = Field(
        ...,
        description="SAFE-to-log: redaction token for PII categories, <=64-char truncation otherwise.",
    )
    action: Action


class CheckResponse(BaseModel):
    """Response shape (identical for ``/check/input`` and ``/check/output``).

    ``confidence`` and ``metadata`` are ADDITIVE: existing callers ignore unknown fields, and
    on benign input with all flags at their defaults ``confidence`` is 1.0 and ``metadata``
    is omitted, so the wire shape is a strict superset of today's.
    """

    decision: Decision
    processed_text: str | None = Field(
        default=None, description="Populated when the decision applied redaction."
    )
    violations: list[Violation] = Field(default_factory=list)
    check_id: str
    duration_ms: int
    trace_id: str
    # Aggregate decision confidence in [0,1] (1.0 = deterministic / fully confident). Lower
    # when an uncertain classifier band or a fail-soft fallback contributed to the decision.
    confidence: float = Field(default=1.0, description="Aggregate decision confidence [0,1].")
    # Additive, open-ended decision metadata (injection risk, groundedness, fail-mode applied,
    # classifier provenance, per-stage timeouts). Omitted (None) when nothing notable to report.
    metadata: dict[str, Any] | None = Field(
        default=None, description="Additive decision metadata (injection/groundedness/etc.)."
    )
