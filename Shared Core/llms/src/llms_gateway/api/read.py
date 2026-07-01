"""Read APIs (WP05 — Contract-19 / Component 1d read surface).

Three authenticated, tenant-scoped read endpoints (prefix ``/v1``):

* ``GET /v1/models`` — models resolvable for the CALLER's tenant (per-tenant +
  platform aliases resolved), each with its provider and capability summary.
* ``GET /v1/usage``  — grouped token sums + request_count over ``usage_records``.
* ``GET /v1/cost``   — grouped ``cost_usd`` sum (+ token totals + request_count).

The tenant is taken ONLY from the JWT Principal (Contract 13) — never from a body
or query param — and the usage/cost aggregations run under RLS via
``llms_gateway.db.read_queries`` (which uses ``in_tenant``). ``group_by`` is
validated against a fixed allowlist (422 on an invalid value) and the result set /
time window are capped (config: ``read_max_result_rows`` / ``read_max_range_days``)
so a tenant can never trigger an unbounded scan. Errors render via the Contract 2
envelope (``ApiError``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request

from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db import read_queries
from ..services.capabilities import capability_registry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["read"])

_DEFAULT_GROUP_BY = "date"


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_group_by(raw: list[str] | None) -> list[str]:
    """Validate the repeated/comma-joined ``group_by`` against the allowlist.

    Accepts either repeated params (``?group_by=model&group_by=date``) or a single
    comma-joined value (``?group_by=model,date``). Raises a Contract 2
    VALIDATION_ERROR (422) on any token outside ``read_queries.GROUP_BY_KEYS``.
    """
    if not raw:
        return [_DEFAULT_GROUP_BY]
    tokens: list[str] = []
    for item in raw:
        tokens.extend(part.strip() for part in item.split(",") if part.strip())
    if not tokens:
        return [_DEFAULT_GROUP_BY]
    invalid = [t for t in tokens if t not in read_queries.GROUP_BY_KEYS]
    if invalid:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid group_by value(s).",
            status_code=422,
            details={"invalid": invalid, "allowed": list(read_queries.GROUP_BY_KEYS)},
        )
    # De-dupe, preserve first-seen order.
    return list(dict.fromkeys(tokens))


def _parse_ts(value: str | None, *, param: str) -> datetime | None:
    """Parse an optional ISO-8601 timestamp; 422 on a malformed value.

    A trailing ``Z`` is accepted (mapped to +00:00). A naive datetime (no tz) is
    assumed to be UTC so range comparisons against ``created_at`` (TIMESTAMPTZ) are
    well-defined.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid '{param}' timestamp; expected ISO-8601.",
            status_code=422,
            details={"param": param, "value": value},
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_window(
    ts_from: datetime | None,
    ts_to: datetime | None,
    *,
    max_range_days: int,
) -> tuple[datetime | None, datetime | None]:
    """Clamp the requested window to ``max_range_days`` to bound the scan.

    * 422 if ``from`` > ``to``.
    * If no ``from`` is given, default it to ``to`` (or now) minus the cap.
    * If ``from`` is older than the cap relative to ``to`` (or now), pull it forward
      to the cap — the read stays bounded without erroring.
    """
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "'from' must be earlier than or equal to 'to'.",
            status_code=422,
            details={"from": ts_from.isoformat(), "to": ts_to.isoformat()},
        )
    upper = ts_to if ts_to is not None else datetime.now(UTC)
    earliest = upper - timedelta(days=max_range_days)
    lower = ts_from if ts_from is not None and ts_from >= earliest else earliest
    return lower, ts_to


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        # No DB wired (e.g. a minimal/unit test app) — a read cannot be served.
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Usage store is not available.",
        )
    return pool


# ── GET /v1/models ───────────────────────────────────────────────────────────
@router.get("/models")
async def list_models(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict[str, Any]]]:
    """List models resolvable for the caller's tenant with capability summaries.

    Models = the DB-authoritative capability catalog (literal model ids) UNION the
    targets of every alias visible to the tenant (its own + platform defaults).
    Each entry carries the aliases that resolve to it for THIS tenant.
    """
    # Aliases visible to the tenant (own + NULL platform), RLS-scoped. Best-effort:
    # if the DB is unreachable we still return the in-process capability catalog.
    aliases_by_model: dict[str, set[str]] = {}
    alias_provider: dict[str, str] = {}
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            rows = await read_queries.fetch_tenant_aliases(pool, principal.tenant_id)
        except Exception as exc:  # noqa: BLE001 — fall back to the cached catalog
            logger.warning("models_alias_fetch_failed", error=str(exc))
            rows = []
        for row in rows:
            aliases_by_model.setdefault(row["model_id"], set()).add(row["alias"])
            alias_provider.setdefault(row["model_id"], row["provider"])

    # Every literal model in the capability registry + every aliased model.
    model_ids = set(capability_registry.list_model_ids()) | set(aliases_by_model)

    data: list[dict[str, Any]] = []
    for model_id in sorted(model_ids):
        cap = capability_registry.get(model_id)
        provider = cap.provider if cap is not None else alias_provider.get(model_id, "")
        capabilities = (
            {
                "max_tokens_cap": cap.max_tokens_cap,
                "context_window": cap.context_window,
                "supports_vision": cap.supports_vision,
                "supports_tools": cap.supports_tools,
                "supports_streaming": cap.supports_streaming,
                "embedding_dim": cap.embedding_dim,
            }
            if cap is not None
            else {}
        )
        data.append(
            {
                "id": model_id,
                "provider": provider,
                "aliases": sorted(aliases_by_model.get(model_id, set())),
                "capabilities": capabilities,
            }
        )
    return {"data": data}


# ── GET /v1/usage ────────────────────────────────────────────────────────────
@router.get("/usage")
async def get_usage(
    request: Request,
    principal: Principal = Depends(require_principal),
    ts_from: str | None = Query(default=None, alias="from"),
    ts_to: str | None = Query(default=None, alias="to"),
    group_by: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregate token usage for the caller's tenant, grouped by ``group_by``."""
    settings = request.app.state.settings
    groups = _parse_group_by(group_by)
    lower, upper = _resolve_window(
        _parse_ts(ts_from, param="from"),
        _parse_ts(ts_to, param="to"),
        max_range_days=settings.read_max_range_days,
    )
    pool = _get_pool(request)
    rows = await read_queries.aggregate_usage(
        pool,
        principal.tenant_id,
        group_by=groups,
        ts_from=lower,
        ts_to=upper,
        limit=settings.read_max_result_rows,
    )
    return {
        "group_by": groups,
        "from": lower.isoformat() if lower is not None else None,
        "to": upper.isoformat() if upper is not None else None,
        "data": [_serialise_row(r) for r in rows],
    }


# ── GET /v1/cost ─────────────────────────────────────────────────────────────
@router.get("/cost")
async def get_cost(
    request: Request,
    principal: Principal = Depends(require_principal),
    ts_from: str | None = Query(default=None, alias="from"),
    ts_to: str | None = Query(default=None, alias="to"),
    group_by: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregate ``cost_usd`` (+ token totals) for the tenant, grouped by ``group_by``."""
    settings = request.app.state.settings
    groups = _parse_group_by(group_by)
    lower, upper = _resolve_window(
        _parse_ts(ts_from, param="from"),
        _parse_ts(ts_to, param="to"),
        max_range_days=settings.read_max_range_days,
    )
    pool = _get_pool(request)
    rows = await read_queries.aggregate_cost(
        pool,
        principal.tenant_id,
        group_by=groups,
        ts_from=lower,
        ts_to=upper,
        limit=settings.read_max_result_rows,
    )
    return {
        "group_by": groups,
        "from": lower.isoformat() if lower is not None else None,
        "to": upper.isoformat() if upper is not None else None,
        "data": [_serialise_row(r) for r in rows],
    }


def _serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe a returned aggregation row (Decimal cost_usd -> float)."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "cost_usd" and value is not None:
            out[key] = float(value)
        else:
            out[key] = value
    return out
