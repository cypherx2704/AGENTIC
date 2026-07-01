"""GET /v1/violations — paginated, RLS-scoped read of the violation log (WP07).

The FRONTEND dependency: a tenant-scoped, cursor-paginated read of
``guardrails.violations`` for the caller's tenant. Returns ONLY redaction-SAFE fields —
``matched`` is the value the pipeline already rendered safe (the redaction TOKEN for PII
categories, or a <=64-char truncation for non-PII), so raw PII never leaves the store.

Query parameters:
  * ``from`` / ``to``  — ISO-8601 timestamps bounding ``created_at`` (inclusive ``from``,
    exclusive ``to``). Both optional; ``from`` defaults to the unix epoch and ``to`` to
    "now" so the endpoint is always answerable.
  * ``agent_id``       — optional filter (UUID).
  * ``decision``       — optional filter (allow | warn | redact | block).
  * ``limit``          — page size (1..200, default 50).
  * ``after_id``       — keyset cursor: the ``id`` of the last row of the previous page.
    Pagination is newest-first by ``(created_at DESC, id DESC)``; ``after_id`` resumes
    strictly BEFORE that row's ``(created_at, id)`` so pages never overlap or skip.

RLS: every read runs inside ``in_tenant`` (sets ``app.tenant_id``), so the tenant filter
is enforced by the database policy, not just the WHERE clause. With no pool configured
(local/unit) the endpoint returns an empty page so the surface is always answerable and
the infra-free tests are unaffected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from psycopg.rows import tuple_row

from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["violations"])

_DECISIONS = ("allow", "warn", "redact", "block")
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# Only redaction-SAFE columns are projected (matched_text is the safe token/truncation).
_SELECT_COLS = (
    "id::text, check_id::text, request_id::text, agent_id::text, task_id::text, "
    "trace_id::text, policy_id::text, direction, decision, rule_id, rule_name, "
    "severity, category, matched_text, created_at"
)


def _parse_ts(value: str | None, field: str, default: datetime) -> datetime:
    if value is None:
        return default
    try:
        # Accept a trailing 'Z' (datetime.fromisoformat handles it on 3.11+).
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid '{field}' timestamp; expected ISO-8601.",
            status_code=400,
            details={"reason": "invalid_timestamp", "field": field},
        ) from exc


def _parse_uuid(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid '{field}'; expected a UUID.",
            status_code=400,
            details={"reason": "invalid_uuid", "field": field},
        ) from exc


def _row_to_violation(r: tuple[Any, ...]) -> dict[str, Any]:
    created_at = r[14]
    return {
        "id": r[0],
        "check_id": r[1],
        "request_id": r[2],
        "agent_id": r[3],
        "task_id": r[4],
        "trace_id": r[5],
        "policy_id": r[6],
        "direction": r[7],
        "decision": r[8],
        "rule_id": r[9],
        "rule_name": r[10],
        "severity": r[11],
        "category": r[12],
        # SAFE: redaction token (PII) or <=64-char truncation (non-PII) — never raw PII.
        "matched": r[13],
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


@router.get("/violations")
async def list_violations(
    request: Request,
    principal: Principal = Depends(require_principal),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None, alias="to"),
    agent_id: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    after_id: str | None = Query(default=None),
) -> dict[str, Any]:
    ts_from = _parse_ts(from_, "from", datetime.fromtimestamp(0, tz=UTC))
    ts_to = _parse_ts(to, "to", datetime.now(tz=UTC))
    agent = _parse_uuid(agent_id, "agent_id")
    cursor = _parse_uuid(after_id, "after_id")
    if decision is not None and decision not in _DECISIONS:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid 'decision' filter.",
            status_code=400,
            details={"reason": "invalid_decision", "allowed": list(_DECISIONS)},
        )

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        # No store configured (local/unit) — always-answerable empty page.
        return {"violations": [], "next_cursor": None, "has_more": False}

    async def _txn(conn: Any) -> list[tuple[Any, ...]]:
        # Build the predicate incrementally; params are bound positionally.
        where = ["created_at >= %s", "created_at < %s"]
        params: list[Any] = [ts_from, ts_to]
        if agent is not None:
            where.append("agent_id = %s")
            params.append(agent)
        if decision is not None:
            where.append("decision = %s")
            params.append(decision)
        if cursor is not None:
            # Keyset: continue strictly before the cursor row's (created_at, id).
            where.append(
                "(created_at, id) < "
                "(SELECT created_at, id FROM guardrails.violations WHERE id = %s)"
            )
            params.append(cursor)
        sql = (
            f"SELECT {_SELECT_COLS} FROM guardrails.violations "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT %s"
        )
        params.append(limit + 1)  # fetch one extra to compute has_more
        cur = await conn.cursor(row_factory=tuple_row).execute(sql, tuple(params))
        return await cur.fetchall()

    try:
        rows = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — read-only; surface a clean 503 on DB error
        logger.warning("list_violations_failed", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Could not read the violation log."
        ) from exc

    has_more = len(rows) > limit
    page = rows[:limit]
    violations = [_row_to_violation(r) for r in page]
    next_cursor = violations[-1]["id"] if (has_more and violations) else None
    return {"violations": violations, "next_cursor": next_cursor, "has_more": has_more}
