"""Custom (tenant-authored) rules CRUD — ``/v1/rules`` (WP07).

Tenant-scoped (RLS) management of the two custom rule types:

* ``regex``                — a user-supplied pattern + category/severity/default_action/direction.
* ``classifier-threshold`` — a category + a threshold against the classifier score.

Endpoints (all under ``/v1``):

* ``POST   /rules``        — create a custom rule (v1). Writes require tenant:admin or
  platform:admin scope. Regex rules pass the SAVE-time ReDoS guard (422 UNSAFE_REGEX on
  failure). 409 when the tenant's active-custom-rule quota is exceeded.
* ``GET    /rules``        — list the tenant's custom rules (active versions; the stable
  ``id`` is ``root_rule_id``). Read-only; any authenticated principal for the tenant.
* ``GET    /rules/{id}``   — fetch one custom rule by its stable id (404 if absent).
* ``PUT    /rules/{id}``   — VERSIONED update: INSERT a new version + RETIRE the old, in one
  tenant transaction. Returns the new active version (same stable ``id``).
* ``DELETE /rules/{id}``   — RETIRE the active version (soft delete; the version chain is
  kept for audit). 204.

The persisted shape + version-chain model live in migration 0004; the executable
definition + ReDoS guard live in ``services/rules/custom.py``. Custom rows are written to
``guardrails.rules`` with a non-NULL ``tenant_id`` (the table is MIXED-scope; RLS admits
NULL platform rows + own-tenant rows on read, own-tenant only on write).

DB-less posture: with no pool configured (local/unit) the endpoints return a clean
SERVICE_UNAVAILABLE for writes (no store to persist to) and an empty list for reads, so
the surface is always answerable and the existing infra-free tests are unaffected.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request, Response
from psycopg.rows import tuple_row
from pydantic import BaseModel, ConfigDict, Field

from ..core.auth import Principal, require_principal
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant
from ..services.rules import (
    CUSTOM_TYPE_CLASSIFIER_THRESHOLD,
    CUSTOM_TYPE_REGEX,
    UnsafeRegexError,
    assert_regex_safe,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["rules"])

# Scopes permitted to WRITE custom rules (create / update / delete). Reads need only the
# baseline guardrails:check scope already enforced by require_principal.
_WRITE_SCOPES = ("tenant:admin", "platform:admin")

_DIRECTIONS = ("input", "output", "both")
_ACTIONS = ("allow", "warn", "redact", "block")
_FAIL_MODES = ("closed", "open")
_SEVERITIES = ("info", "low", "medium", "high", "critical")
_STATUS_RETIRED = "retired"
_STATUS_ACTIVE = "active"


# ── Request / response models ──────────────────────────────────────────────────
class CustomRuleCreate(BaseModel):
    """Body of ``POST /v1/rules`` / ``PUT /v1/rules/{id}``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    type: Literal["regex", "classifier-threshold"] = Field(
        ..., description="'regex' (pattern) or 'classifier-threshold' (category + threshold)."
    )
    direction: Literal["input", "output", "both"] = "input"
    category: str = Field(..., min_length=1, max_length=50)
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    default_action: Literal["allow", "warn", "redact", "block"] = "block"
    default_fail_mode: Literal["closed", "open"] = "closed"
    timeout_ms: int = Field(default=10, ge=1, le=5000)
    # regex type:
    pattern: str | None = Field(default=None, description="Regex source (required for type=regex).")
    # classifier-threshold type:
    classifier_category: str | None = Field(
        default=None, description="Target classifier category (required for classifier-threshold)."
    )
    threshold: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Score threshold (required for classifier-threshold)."
    )


class CustomRule(BaseModel):
    """A custom rule as returned by the API (stable ``id`` = root_rule_id)."""

    id: str
    rule_id: str  # the concrete active-version rule_id (what the pipeline evaluates)
    tenant_id: str
    version: int
    name: str
    type: str
    direction: str
    category: str
    severity: str
    default_action: str
    default_fail_mode: str
    timeout_ms: int
    status: str
    pattern: str | None = None
    classifier_category: str | None = None
    threshold: float | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────
def _require_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Custom rules require a configured datastore.",
            details={"reason": "no_datastore"},
        )
    return pool


def _require_write_scope(principal: Principal) -> None:
    if not any(s in principal.scopes for s in _WRITE_SCOPES):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Writing custom rules requires tenant:admin or platform:admin scope.",
            details={"required_any": list(_WRITE_SCOPES)},
        )


def _resolve_quota(principal: Principal, settings: Settings) -> int:
    """Resolve the tenant's active-custom-rule limit (Auth/Contract-19).

    Authoritative source is the principal's ``limits.custom_rules_max`` claim when present;
    otherwise the configured default. A value <= 0 means UNCAPPED (fail-open). If the claim
    is malformed we fall back to the configured default (never harder than configured).
    """
    claims = principal.raw_claims or {}
    limits = claims.get("limits")
    if isinstance(limits, dict) and "custom_rules_max" in limits:
        try:
            return int(limits["custom_rules_max"])
        except (TypeError, ValueError):
            logger.warning("custom_rules_quota_claim_malformed", value=limits.get("custom_rules_max"))
    return settings.custom_rules_max


def _validate_definition(body: CustomRuleCreate, settings: Settings) -> None:
    """Type-specific validation incl. the SAVE-time ReDoS guard for regex rules."""
    if body.type == CUSTOM_TYPE_REGEX:
        if not body.pattern:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "A regex custom rule requires a 'pattern'.",
                details={"reason": "missing_pattern"},
            )
        try:
            assert_regex_safe(
                body.pattern,
                max_length=settings.custom_rule_regex_max_length,
                budget_ms=settings.custom_rule_regex_budget_ms,
            )
        except UnsafeRegexError as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "The supplied regex failed the safety (ReDoS) check.",
                details={"reason": exc.reason},
            ) from exc
    elif body.type == CUSTOM_TYPE_CLASSIFIER_THRESHOLD:
        if not body.classifier_category or body.threshold is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "A classifier-threshold rule requires 'classifier_category' and 'threshold'.",
                details={"reason": "missing_threshold"},
            )


def _new_root_id() -> str:
    """Mint a stable, namespaced root id for a new custom rule."""
    return f"custom-{uuid.uuid4()}"


def _version_rule_id(root_id: str, version: int) -> str:
    """The concrete per-version rule_id (RULES_BY_ID key the pipeline evaluates)."""
    return f"{root_id}:v{version}"


_SELECT_COLS = (
    "rule_id, root_rule_id, tenant_id::text, version, name, custom_type, direction, "
    "default_category, default_severity, default_action, default_fail_mode, timeout_ms, "
    "status, pattern, classifier_category, threshold"
)


def _row_to_model(r: tuple[Any, ...]) -> CustomRule:
    return CustomRule(
        id=r[1],
        rule_id=r[0],
        tenant_id=r[2],
        version=int(r[3]) if str(r[3]).isdigit() else 1,
        name=r[4] or r[0],
        type=r[5],
        direction=r[6],
        category=r[7],
        severity=r[8],
        default_action=r[9],
        default_fail_mode=r[10],
        timeout_ms=r[11],
        status=r[12],
        pattern=r[13],
        classifier_category=r[14],
        threshold=float(r[15]) if r[15] is not None else None,
    )


def _invalidate_cache(request: Request, tenant_id: str) -> None:
    loader = getattr(request.app.state, "custom_rule_loader", None)
    if loader is not None:
        loader.invalidate(tenant_id)


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.post("/rules", status_code=201)
async def create_rule(
    body: CustomRuleCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    settings = get_settings()
    _require_write_scope(principal)
    pool = _require_pool(request)
    _validate_definition(body, settings)

    root_id = _new_root_id()
    version = 1
    version_rule_id = _version_rule_id(root_id, version)
    limit = _resolve_quota(principal, settings)

    async def _txn(conn: Any) -> CustomRule:
        # Quota: count the tenant's ACTIVE custom rules (fail-open when limit <= 0).
        if limit > 0:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT COUNT(*) FROM guardrails.rules
                 WHERE tenant_id = %s AND custom_type IS NOT NULL AND status <> %s
                """,
                (principal.tenant_id, _STATUS_RETIRED),
            )
            (active_count,) = await cur.fetchone()
            if active_count >= limit:
                raise ApiError(
                    ErrorCode.RATE_LIMIT_EXCEEDED,
                    "Active custom-rule quota exceeded for this tenant.",
                    status_code=409,
                    details={"reason": "custom_rules_max", "limit": limit, "active": active_count},
                )

        cur = await conn.cursor(row_factory=tuple_row).execute(
            f"""
            INSERT INTO guardrails.rules
              (rule_id, root_rule_id, tenant_id, version, name, custom_type, direction,
               default_action, default_fail_mode, default_stream_mode, default_severity,
               default_category, timeout_ms, status, pattern, classifier_category, threshold,
               created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'buffer',%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING {_SELECT_COLS}
            """,
            (
                version_rule_id, root_id, principal.tenant_id, str(version), body.name,
                body.type, body.direction, body.default_action, body.default_fail_mode,
                body.severity, body.category, body.timeout_ms, _STATUS_ACTIVE,
                body.pattern, body.classifier_category, body.threshold,
                principal.agent_id,
            ),
        )
        return _row_to_model(await cur.fetchone())

    try:
        model = await in_tenant(pool, principal.tenant_id, _txn)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface a clean 503 on a DB write failure
        logger.error("custom_rule_create_failed", error=str(exc))
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Could not persist the custom rule.") from exc

    _invalidate_cache(request, principal.tenant_id)
    logger.info("custom_rule_created", rule_id=model.rule_id, root=model.id, type=model.type)
    return {"rule": model.model_dump()}


@router.get("/rules")
async def list_rules(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return {"rules": []}

    async def _txn(conn: Any) -> list[tuple[Any, ...]]:
        cur = await conn.cursor(row_factory=tuple_row).execute(
            f"""
            SELECT {_SELECT_COLS}
              FROM guardrails.rules
             WHERE tenant_id = %s AND custom_type IS NOT NULL AND status <> %s
             ORDER BY name
            """,
            (principal.tenant_id, _STATUS_RETIRED),
        )
        return await cur.fetchall()

    try:
        rows = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — read-only; empty list on DB error
        logger.warning("custom_rule_list_fallback", error=str(exc))
        return {"rules": []}

    return {"rules": [_row_to_model(r).model_dump() for r in rows]}


async def _fetch_active(pool: Any, tenant_id: str, root_id: str) -> tuple[Any, ...] | None:
    async def _txn(conn: Any) -> tuple[Any, ...] | None:
        cur = await conn.cursor(row_factory=tuple_row).execute(
            f"""
            SELECT {_SELECT_COLS}
              FROM guardrails.rules
             WHERE tenant_id = %s AND root_rule_id = %s AND status <> %s
             LIMIT 1
            """,
            (tenant_id, root_id, _STATUS_RETIRED),
        )
        return await cur.fetchone()

    return await in_tenant(pool, tenant_id, _txn)


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    pool = _require_pool(request)
    try:
        row = await _fetch_active(pool, principal.tenant_id, rule_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("custom_rule_get_failed", error=str(exc))
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Could not read the custom rule.") from exc
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Custom rule not found.")
    return {"rule": _row_to_model(row).model_dump()}


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: CustomRuleCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    settings = get_settings()
    _require_write_scope(principal)
    pool = _require_pool(request)
    _validate_definition(body, settings)

    async def _txn(conn: Any) -> CustomRule | None:
        # Read the current active version (locks the chain for the repoint).
        cur = await conn.cursor(row_factory=tuple_row).execute(
            """
            SELECT rule_id, version FROM guardrails.rules
             WHERE tenant_id = %s AND root_rule_id = %s AND status <> %s
             FOR UPDATE
            """,
            (principal.tenant_id, rule_id, _STATUS_RETIRED),
        )
        current = await cur.fetchone()
        if current is None:
            return None
        prev_rule_id, prev_version = current[0], int(current[1]) if str(current[1]).isdigit() else 1
        new_version = prev_version + 1
        new_rule_id = _version_rule_id(rule_id, new_version)

        # Retire the old version, then insert the new active version (append-only chain).
        await conn.execute(
            "UPDATE guardrails.rules SET status = %s, updated_at = NOW() "
            "WHERE tenant_id = %s AND rule_id = %s",
            (_STATUS_RETIRED, principal.tenant_id, prev_rule_id),
        )
        cur = await conn.cursor(row_factory=tuple_row).execute(
            f"""
            INSERT INTO guardrails.rules
              (rule_id, root_rule_id, previous_rule_id, tenant_id, version, name, custom_type,
               direction, default_action, default_fail_mode, default_stream_mode,
               default_severity, default_category, timeout_ms, status, pattern,
               classifier_category, threshold, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'buffer',%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING {_SELECT_COLS}
            """,
            (
                new_rule_id, rule_id, prev_rule_id, principal.tenant_id, str(new_version),
                body.name, body.type, body.direction, body.default_action, body.default_fail_mode,
                body.severity, body.category, body.timeout_ms, _STATUS_ACTIVE,
                body.pattern, body.classifier_category, body.threshold, principal.agent_id,
            ),
        )
        return _row_to_model(await cur.fetchone())

    try:
        model = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001
        logger.error("custom_rule_update_failed", error=str(exc))
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Could not update the custom rule.") from exc
    if model is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Custom rule not found.")

    _invalidate_cache(request, principal.tenant_id)
    logger.info("custom_rule_updated", root=model.id, version=model.version)
    return {"rule": model.model_dump()}


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    _require_write_scope(principal)
    pool = _require_pool(request)

    async def _txn(conn: Any) -> int:
        cur = await conn.execute(
            """
            UPDATE guardrails.rules SET status = %s, updated_at = NOW()
             WHERE tenant_id = %s AND root_rule_id = %s AND status <> %s
            """,
            (_STATUS_RETIRED, principal.tenant_id, rule_id, _STATUS_RETIRED),
        )
        return cur.rowcount

    try:
        affected = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001
        logger.error("custom_rule_delete_failed", error=str(exc))
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Could not retire the custom rule.") from exc
    if not affected:
        raise ApiError(ErrorCode.NOT_FOUND, "Custom rule not found.")

    _invalidate_cache(request, principal.tenant_id)
    logger.info("custom_rule_retired", root=rule_id)
    return Response(status_code=204)
