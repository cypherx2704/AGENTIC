"""Policy authoring + simulation (WP07) — ``/v1/policies`` CRUD, assignment, simulation.

Read-only listing (``GET /v1/policies``) keeps its first-cycle behaviour: with no DB it
returns the built-in platform default so the endpoint is always answerable. The WP07
additions are DB-backed and degrade to 503 SERVICE_UNAVAILABLE when no DB pool is
configured (local/unit), so the existing check pipeline + its tests are unaffected.

Authoring contract (amended plan):

* ``POST /v1/policies``            — create with SAVE-TIME validation (422 on invalid).
* ``PUT  /v1/policies/{id}``       — APPEND-ONLY edit: a new version is inserted and the
                                     active pointer flips atomically (never mutate a
                                     published row). ``fail_mode_override`` changes are
                                     AUDITED (policy_audit row + policy.changed event).
* ``GET  /v1/policies/{id}``       — the active version + the full version chain.
* ``GET  /v1/policies``            — list (read-only; built-in fallback).
* ``POST /v1/policies/{id}/assign``— atomic agent -> policy repoint.
* ``POST /v1/policies/{id}/simulate`` — run text through a STORED policy, no real
                                     violation persisted; returns decision + evaluation_trace.
* ``POST /v1/policies/simulate``   — same, against an INLINE draft policy (no stored id).

Writes require scope ``tenant:admin`` or ``platform:admin`` (403 otherwise); reads use the
existing ``require_principal`` dependency (any authenticated principal in tenant scope).
``tenant_id`` / ``agent_id`` come from the JWT only (Contract 13) — never the body.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from psycopg.rows import tuple_row
from pydantic import BaseModel, ConfigDict, Field

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db.outbox import UsageWrite, record_usage
from ..db.pool import in_tenant
from ..services.pipeline import PipelineResult, RuleTraceEntry, evaluate
from ..services.policy_engine import (
    EffectivePolicy,
    EnabledRule,
    PlatformPolicyImmutableError,
    PolicyEngine,
    PolicyNotFoundError,
    builtin_platform_default,
    validate_draft,
)
from ..services.rules import RuleContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["policies"])

# Scopes that may author/assign policies. tenant:admin manages own-tenant policies;
# platform:admin is the elevated operator scope. (Reads need neither.)
_WRITE_SCOPES = ("tenant:admin", "platform:admin")
# Lua: atomic INCR + first-write EXPIRE for the fixed-window sim-rate counter. Returns the
# post-increment count so the caller compares it to the configured per-hour cap.
_SIM_RATE_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""


# ── Request models ──────────────────────────────────────────────────────────────
class RuleConfig(BaseModel):
    """One enabled rule in a policy draft."""

    model_config = ConfigDict(extra="forbid")
    rule_id: str
    enabled: bool = True
    action_override: str | None = None


class PolicyDraft(BaseModel):
    """Body for create / edit / inline-simulate."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=255)
    rules: list[RuleConfig] = Field(default_factory=list)
    stream_mode: str = "buffer"
    fail_mode_override: str | None = None


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str


class SimulateBody(BaseModel):
    """Body for ``POST /v1/policies/{id}/simulate`` (text only; policy comes from the id)."""

    model_config = ConfigDict(extra="forbid")
    text: str
    input_text: str | None = None
    direction: str = "input"
    as_of: str | None = Field(default=None, description="ISO-8601; resolve the version active then.")


class SimulateDraftBody(SimulateBody):
    """Body for ``POST /v1/policies/simulate`` — carries an inline draft policy."""

    policy: PolicyDraft


# ── Helpers ───────────────────────────────────────────────────────────────────────
def _builtin_response() -> dict[str, Any]:
    policy = builtin_platform_default()
    return {
        "policies": [
            {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "tenant_id": None,
                "is_default": True,
                "status": "active",
                "rules": [{"rule_id": r.rule_id, "enabled": True} for r in policy.rules],
            }
        ]
    }


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _engine(request: Request) -> PolicyEngine:
    engine = getattr(request.app.state, "policy_engine", None)
    return engine if engine is not None else PolicyEngine(None)


def _validate_policy_id(policy_id: str) -> str:
    """Validate a path ``policy_id`` is a UUID before it binds to a uuid column.

    A non-UUID id can never identify a stored policy, so it is surfaced as 404 NOT_FOUND
    (mirroring the engine's PolicyNotFoundError mapping) rather than letting the raw string
    reach Postgres and raise an uncaught 500 (get/simulate) or a caught-as-503 (edit/assign).
    """
    try:
        return str(uuid.UUID(policy_id))
    except ValueError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' not found.") from exc


def _validate_uuid_body(value: str, field: str) -> str:
    """Validate a UUID-typed BODY field up front -> 422 VALIDATION_ERROR (like violations._parse_uuid).

    Keeps a malformed ``agent_id`` from binding to a uuid column (which would otherwise be
    caught by the engine's broad except and reported as a misleading 503).
    """
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid '{field}'; expected a UUID.",
            status_code=422,
            details={"reason": "invalid_uuid", "field": field},
        ) from exc


def _require_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Policy authoring requires the database, which is not configured.",
            status_code=503,
        )
    return pool


def _require_write_scope(principal: Principal) -> None:
    if not any(s in principal.scopes for s in _WRITE_SCOPES):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"Policy authoring requires one of scopes {list(_WRITE_SCOPES)}.",
        )


def _validate_or_422(draft: PolicyDraft) -> list[dict[str, Any]]:
    rules = [r.model_dump() for r in draft.rules]
    issues = validate_draft(
        name=draft.name,
        rules=rules,
        stream_mode=draft.stream_mode,
        fail_mode_override=draft.fail_mode_override,
    )
    if issues:
        metrics.policy_writes_total.labels("validate", "invalid").inc()
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Policy draft failed validation.",
            status_code=422,
            details={"reason": "invalid_policy", "issues": issues},
        )
    return rules


def _redaction_key(request: Request, tenant_id: str) -> str:
    resolver = getattr(request.app.state, "redaction_resolver", None)
    if resolver is not None:
        return str(resolver.resolve(tenant_id))
    return str(_settings(request).redaction_hmac_key_platform)


def _trace_payload(trace_entries: list[RuleTraceEntry] | None) -> list[dict[str, Any]]:
    if not trace_entries:
        return []
    return [
        {
            "rule_id": e.rule_id,
            "rule_name": e.rule_name,
            "direction": e.direction,
            "evaluated": e.evaluated,
            "matched": e.matched,
            "action": e.action,
            "effective_fail_mode": e.effective_fail_mode,
            "timed_out": e.timed_out,
            "timing_ms": e.timing_ms,
            "hit_count": e.hit_count,
            "matched_samples": e.matched_samples,
        }
        for e in trace_entries
    ]


# ── List (read-only; built-in fallback — unchanged contract) ──────────────────────
@router.get("/policies")
async def list_policies(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return _builtin_response()

    async def _txn(conn: object) -> list[tuple[Any, ...]]:
        cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
            """
            SELECT COALESCE(root_policy_id, policy_id)::text, name, tenant_id::text,
                   is_default, status, rules
              FROM guardrails.policies
             WHERE status = 'active'
             ORDER BY tenant_id NULLS LAST, name
            """
        )
        return await cur.fetchall()

    try:
        rows = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — read-only; fall back to built-in on DB error
        logger.warning("list_policies_fallback", error=str(exc))
        return _builtin_response()

    return {
        "policies": [
            {
                "policy_id": pid,
                "name": name,
                "tenant_id": tid,
                "is_default": is_default,
                "status": status,
                "rules": rules,
            }
            for (pid, name, tid, is_default, status, rules) in rows
        ]
    }


# ── Create ──────────────────────────────────────────────────────────────────────
@router.post("/policies", status_code=201)
async def create_policy(
    request: Request,
    body: PolicyDraft,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    _require_write_scope(principal)
    _require_pool(request)  # presence check (the engine uses app.state.db_pool)
    rules = _validate_or_422(body)
    try:
        row = await _engine(request).create_policy(
            tenant_id=principal.tenant_id,
            name=body.name,
            rules=rules,
            stream_mode=body.stream_mode,
            fail_mode_override=body.fail_mode_override,
            actor_agent_id=principal.agent_id,
            request_id=trace.request_id_var.get(),
            trace_id=trace.trace_id_var.get(),
            producer_version=_settings(request).service_version,
        )
    except Exception as exc:  # noqa: BLE001
        metrics.policy_writes_total.labels("create", "error").inc()
        logger.error("policy_create_failed", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Failed to create policy.", status_code=503
        ) from exc
    metrics.policy_writes_total.labels("create", "ok").inc()
    return JSONResponse(status_code=201, content={"policy": row})


# ── Edit (append-only) ────────────────────────────────────────────────────────────
@router.put("/policies/{policy_id}")
async def edit_policy(
    request: Request,
    policy_id: str,
    body: PolicyDraft,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    _require_write_scope(principal)
    policy_id = _validate_policy_id(policy_id)
    _require_pool(request)
    rules = _validate_or_422(body)
    try:
        row = await _engine(request).edit_policy(
            tenant_id=principal.tenant_id,
            root_policy_id=policy_id,
            name=body.name,
            rules=rules,
            stream_mode=body.stream_mode,
            fail_mode_override=body.fail_mode_override,
            actor_agent_id=principal.agent_id,
            request_id=trace.request_id_var.get(),
            trace_id=trace.trace_id_var.get(),
            producer_version=_settings(request).service_version,
        )
    except PolicyNotFoundError as exc:
        metrics.policy_writes_total.labels("edit", "not_found").inc()
        raise ApiError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' not found.") from exc
    except PlatformPolicyImmutableError as exc:
        metrics.policy_writes_total.labels("edit", "invalid").inc()
        raise ApiError(
            ErrorCode.FORBIDDEN, "Platform policies cannot be edited by a tenant."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        metrics.policy_writes_total.labels("edit", "error").inc()
        logger.error("policy_edit_failed", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Failed to edit policy.", status_code=503
        ) from exc
    metrics.policy_writes_total.labels("edit", "ok").inc()
    return {"policy": row}


# ── Get (with versions) ──────────────────────────────────────────────────────────
@router.get("/policies/{policy_id}")
async def get_policy(
    request: Request,
    policy_id: str,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    policy_id = _validate_policy_id(policy_id)
    _require_pool(request)
    try:
        result = await _engine(request).get_policy(
            tenant_id=principal.tenant_id, root_policy_id=policy_id
        )
    except PolicyNotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' not found.") from exc
    return result


# ── Assign (atomic agent repoint) ──────────────────────────────────────────────────
@router.post("/policies/{policy_id}/assign")
async def assign_policy(
    request: Request,
    policy_id: str,
    body: AssignBody,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    _require_write_scope(principal)
    policy_id = _validate_policy_id(policy_id)
    agent_id = _validate_uuid_body(body.agent_id, "agent_id")
    _require_pool(request)
    try:
        result = await _engine(request).assign_policy(
            tenant_id=principal.tenant_id,
            agent_id=agent_id,
            root_policy_id=policy_id,
            actor_agent_id=principal.agent_id,
            request_id=trace.request_id_var.get(),
            trace_id=trace.trace_id_var.get(),
            producer_version=_settings(request).service_version,
        )
    except PolicyNotFoundError as exc:
        metrics.policy_writes_total.labels("assign", "not_found").inc()
        raise ApiError(ErrorCode.NOT_FOUND, f"Policy '{policy_id}' not found.") from exc
    except PlatformPolicyImmutableError as exc:
        metrics.policy_writes_total.labels("assign", "invalid").inc()
        raise ApiError(
            ErrorCode.FORBIDDEN, "A platform policy cannot be agent-assigned here."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        metrics.policy_writes_total.labels("assign", "error").inc()
        logger.error("policy_assign_failed", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Failed to assign policy.", status_code=503
        ) from exc
    metrics.policy_writes_total.labels("assign", "ok").inc()
    return {"assignment": result}


# ── Simulation ────────────────────────────────────────────────────────────────────
async def _enforce_sim_rate_limit(request: Request, tenant_id: str) -> None:
    """Per-tenant sim/hour limit (fixed 1-hour Valkey window). FAILS OPEN on any trouble.

    429 RATE_LIMIT_EXCEEDED when the count exceeds the cap. With no Valkey client wired,
    a disabled limit (<=0), or a backend error/timeout, the call is allowed (fail-open) so
    a cache outage never blocks authoring.
    """
    settings = _settings(request)
    cap = settings.simulation_rate_limit_per_hour
    if cap <= 0:
        return
    valkey = getattr(request.app.state, "valkey", None)
    if valkey is None:
        return
    hour_epoch = int(time.time()) // 3600
    key = f"{settings.simulation_rate_limit_key_prefix}{tenant_id}:{hour_epoch}"
    try:
        count = await valkey.eval(
            _SIM_RATE_LUA,
            keys=[key],
            args=[3600],
            timeout_seconds=settings.simulation_rate_limit_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — fail OPEN: cache trouble never blocks authoring
        logger.warning("sim_rate_limit_skipped", reason="valkey_error", error=str(exc))
        return
    if int(count) > cap:
        metrics.simulation_rate_limited_total.inc()
        raise ApiError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            "Simulation rate limit exceeded for this tenant.",
            details={"limit_per_hour": cap},
        )


async def _run_simulation(
    request: Request,
    principal: Principal,
    *,
    policy: EffectivePolicy,
    source: str,
    text: str,
    input_text: str | None,
    direction: str,
) -> JSONResponse:
    if direction not in ("input", "output"):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "direction must be 'input' or 'output'.", status_code=422
        )
    max_chars = _settings(request).simulation_max_text_chars
    if len(text) > max_chars:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Simulation text exceeds the {max_chars}-character limit.",
            status_code=422,
        )

    classifier = getattr(request.app.state, "classifier", None)
    ctx = RuleContext(
        classifier=classifier, input_text=input_text if direction == "output" else None
    )
    started = time.monotonic()
    result: PipelineResult = evaluate(
        text=text,
        policy=policy,
        direction=direction,
        tenant_id=principal.tenant_id,
        redaction_key=_redaction_key(request, principal.tenant_id),
        ctx=ctx,
        trace=True,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    rules_evaluated = sum(1 for e in (result.trace or []) if e.evaluated)

    # Meter the simulation as operation='simulate', cost 0 (NEVER billed; no violation rows).
    await _record_simulate_usage(
        request,
        principal,
        input_bytes=len(text.encode("utf-8")),
        rules_evaluated=rules_evaluated,
        duration_ms=duration_ms,
    )

    metrics.simulations_total.labels(source, result.decision).inc()
    return JSONResponse(
        content={
            "decision": result.decision,
            "processed_text": result.processed_text,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "rule_name": v.rule_name,
                    "severity": v.severity,
                    "category": v.category,
                    "matched": v.matched,
                    "action": v.action,
                }
                for v in result.violations
            ],
            "evaluation_trace": _trace_payload(result.trace),
            "policy_id": policy.policy_id,
            "policy_name": policy.name,
            "duration_ms": duration_ms,
            "simulated": True,
            "trace_id": trace.trace_id_var.get(),
        }
    )


async def _record_simulate_usage(
    request: Request,
    principal: Principal,
    *,
    input_bytes: int,
    rules_evaluated: int,
    duration_ms: int,
) -> None:
    """Emit a simulate usage event (fail-soft). No-op without a DB pool."""
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return
    settings = _settings(request)
    usage = UsageWrite(
        tenant_id=principal.tenant_id,
        operation=settings.simulation_usage_operation,
        request_id=trace.request_id_var.get(),
        trace_id=trace.trace_id_var.get(),
        agent_id=principal.agent_id,
        api_key_id=principal.api_key_id,
        input_bytes=input_bytes,
        rules_evaluated=rules_evaluated,
        cost_usd=0.0,
        duration_ms=duration_ms,
    )
    try:
        await record_usage(pool, usage, producer_version=settings.service_version)
    except Exception as exc:  # noqa: BLE001 — metering is best-effort, never fails a simulate
        logger.warning("simulate_usage_write_failed", error=str(exc))


@router.post("/policies/{policy_id}/simulate", response_model=None)
async def simulate_stored_policy(
    request: Request,
    policy_id: str,
    body: SimulateBody,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    policy_id = _validate_policy_id(policy_id)
    _require_pool(request)
    await _enforce_sim_rate_limit(request, principal.tenant_id)
    try:
        policy = await _engine(request).resolve_for_simulation(
            tenant_id=principal.tenant_id, root_policy_id=policy_id, as_of=body.as_of
        )
    except PolicyNotFoundError as exc:
        raise ApiError(
            ErrorCode.NOT_FOUND, f"Policy '{policy_id}' not found (or no version at as_of)."
        ) from exc
    return await _run_simulation(
        request, principal, policy=policy, source="stored",
        text=body.text, input_text=body.input_text, direction=body.direction,
    )


@router.post("/policies/simulate", response_model=None)
async def simulate_draft_policy(
    request: Request,
    body: SimulateDraftBody,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    # Inline-draft simulation needs no DB (nothing is stored); still rate-limited + validated.
    await _enforce_sim_rate_limit(request, principal.tenant_id)
    rules = _validate_or_422(body.policy)
    policy = EffectivePolicy(
        policy_id="draft",
        name=body.policy.name,
        rules=tuple(
            EnabledRule(r["rule_id"], r.get("action_override"))
            for r in rules
            if r.get("enabled", True) is not False
        ),
        fail_mode_override=body.policy.fail_mode_override,
    )
    return await _run_simulation(
        request, principal, policy=policy, source="draft",
        text=body.text, input_text=body.input_text, direction=body.direction,
    )
