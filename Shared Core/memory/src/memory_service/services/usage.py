"""Contract-19.1 usage-metering payload builder for ``cypherx.memory.usage.recorded``.

Memory emits a usage event on store / search / delete so the platform's metering pipeline
can bill memory operations (this fixes the previously-missing Contract-19 usage event).
The payload shape is ``contracts/kafka/events/memory.usage.recorded.schema.json`` and is
wrapped by the Contract-5 envelope when it lands in the outbox.

Required by the contract: ``tenant_id``, ``operation``, ``units`` (>=1 numeric entry).
Optional: ``api_key_id``, ``agent_id``, ``principal_id``, ``cost_usd``, ``duration_ms``,
``request_id``, ``trace_id``. The contract sets ``additionalProperties: true`` so newer
producers stay forward-compatible — we only add fields we actually have.

This module is PURE (no DB, no network); the repos/API call it and hand the payload to
``outbox.emit`` on the live transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.auth import Principal

OP_WRITE = "write"
OP_RECALL = "recall"
OP_DELETE = "delete"
OP_SCORE = "score"


def build_usage_payload(
    *,
    principal: Principal,
    operation: str,
    units: dict[str, float],
    trace_id: str | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """Build a Contract-19.1 ``memory.usage.recorded`` payload from a caller + counters.

    ``units`` MUST be non-empty with numeric values (the contract requires >=1 entry).
    Identity fields are taken ONLY from the resolved ``Principal`` (never a request body),
    matching Contract-13. Agent/principal ids may be non-UUID text in this service; we pass
    them through as the contract tolerates unknown shapes (``additionalProperties: true``).
    """
    payload: dict[str, Any] = {
        "tenant_id": principal.tenant_id,
        "operation": operation,
        "units": {k: float(v) for k, v in units.items()},
    }
    if not payload["units"]:
        # Defensive: the contract requires >=1 unit. Record a zero-count so the event is
        # still valid rather than dropping metering entirely.
        payload["units"] = {f"{operation}_ops": 1.0}

    if principal.api_key_id:
        payload["api_key_id"] = principal.api_key_id
    if principal.agent_id:
        payload["agent_id"] = principal.agent_id
    # principal_id is the human/service user for non-agent callers (per the contract).
    if principal.user_id:
        payload["principal_id"] = principal.user_id
    if trace_id:
        payload["trace_id"] = trace_id
    if duration_ms is not None:
        payload["duration_ms"] = int(max(0, duration_ms))
    if cost_usd is not None:
        payload["cost_usd"] = float(max(0.0, cost_usd))
    return payload
