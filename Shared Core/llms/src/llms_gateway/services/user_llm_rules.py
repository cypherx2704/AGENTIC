"""Tenant-defined LLM rules — the per-tenant governance the user owns (Phase 4).

A tenant owner declares rules over ``(provider, model_id)`` pairs:

* ``rule_type = block``         — that model is refused for the tenant (403 LLM_RULE_BLOCKED).
* ``rule_type = allow``         — when ANY allow rule exists, ONLY allowed models may be used
                                  (allowlist semantics); models with no rule are then refused.
* ``can_be_used_by_agents``     — when false, an AGENT principal may not use the model (direct
                                  user invocations still can).
* ``billing_bypass`` / ``is_user_added`` — usage of the model is NOT metered (no usage row, no
                                  billing/usage Kafka events). Returned by :func:`check_rules`.

Enforced in the gateway BEFORE alias resolution side effects. Reads use a short Valkey TTL cache
so the hot chat path doesn't hit Postgres on every call. Writes require tenant:admin (controller).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from psycopg.rows import dict_row

from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


async def _fetch_rules(pool: AsyncConnectionPool, tenant_id: str) -> list[dict[str, Any]]:
    async def _q(conn: Any) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT rule_id::text, provider, model_id, rule_type, can_be_used_by_agents, "
            "       is_user_added, billing_bypass, created_at "
            "  FROM llms.user_llm_rules ORDER BY created_at DESC",
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _q)


async def check_rules(
    pool: AsyncConnectionPool | None,
    tenant_id: str,
    *,
    provider: str,
    model_id: str,
    principal_type: str,
) -> bool:
    """Enforce the tenant's LLM rules for ``(provider, model_id)``; return ``billing_bypass``.

    Raises 403 ``LLM_RULE_BLOCKED`` on a block rule, on an allowlist miss (allow rules exist but
    this model has none), or when an agent principal hits a ``can_be_used_by_agents = false`` model.
    No pool / no rules configured = unrestricted, metered (returns False). Fail-soft on a lookup
    blip (Postgres is the readiness gate, so a genuine outage fails the request elsewhere)."""
    if pool is None:
        return False
    try:
        rules = await _fetch_rules(pool, tenant_id)
    except Exception as exc:  # noqa: BLE001 — never 5xx the call on a rules lookup blip
        logger.warning("user_llm_rules_lookup_failed", tenant_id=tenant_id, error=str(exc))
        return False
    if not rules:
        return False

    match = next((r for r in rules if r["provider"] == provider and r["model_id"] == model_id), None)
    has_allow = any(r["rule_type"] == "allow" for r in rules)

    if match is not None and match["rule_type"] == "block":
        raise ApiError(
            ErrorCode.LLM_RULE_BLOCKED,
            f"Model '{model_id}' is blocked by tenant policy.",
            details={"provider": provider, "model_id": model_id},
        )
    # Allowlist semantics: once any allow rule exists, an unlisted model is refused.
    if has_allow and (match is None or match["rule_type"] != "allow"):
        raise ApiError(
            ErrorCode.LLM_RULE_BLOCKED,
            f"Model '{model_id}' is not in the tenant's allowed LLM list.",
            details={"provider": provider, "model_id": model_id},
        )
    is_agent = principal_type in ("agent", "service")
    if match is not None and is_agent and not match["can_be_used_by_agents"]:
        raise ApiError(
            ErrorCode.LLM_RULE_BLOCKED,
            f"Model '{model_id}' may not be used by agents (tenant policy).",
            details={"provider": provider, "model_id": model_id},
        )
    return bool(match["billing_bypass"]) if match is not None else False


# ── CRUD (tenant:admin enforced at the controller) ─────────────────────────────────────────
async def list_rules(pool: AsyncConnectionPool, tenant_id: str) -> list[dict[str, Any]]:
    return await _fetch_rules(pool, tenant_id)


async def upsert_rule(
    pool: AsyncConnectionPool,
    tenant_id: str,
    created_by: str,
    *,
    provider: str,
    model_id: str,
    rule_type: str,
    can_be_used_by_agents: bool,
    billing_bypass: bool,
    is_user_added: bool,
) -> dict[str, Any]:
    async def _q(conn: Any) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO llms.user_llm_rules "
            "  (tenant_id, provider, model_id, rule_type, can_be_used_by_agents, "
            "   billing_bypass, is_user_added, created_by) "
            "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid) "
            "ON CONFLICT (tenant_id, provider, model_id) DO UPDATE SET "
            "  rule_type = EXCLUDED.rule_type, "
            "  can_be_used_by_agents = EXCLUDED.can_be_used_by_agents, "
            "  billing_bypass = EXCLUDED.billing_bypass, "
            "  is_user_added = EXCLUDED.is_user_added, "
            "  updated_at = NOW() "
            "RETURNING rule_id::text, provider, model_id, rule_type, can_be_used_by_agents, "
            "          is_user_added, billing_bypass, created_at",
            (
                tenant_id, provider, model_id, rule_type, can_be_used_by_agents,
                billing_bypass, is_user_added, created_by,
            ),
        )
        row = await cur.fetchone()
        if row is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "Rule upsert returned no row.")
        return row

    return await in_tenant(pool, tenant_id, _q)


async def delete_rule(pool: AsyncConnectionPool, tenant_id: str, rule_id: str) -> None:
    async def _q(conn: Any) -> None:
        await conn.execute(
            "DELETE FROM llms.user_llm_rules WHERE tenant_id = %s::uuid AND rule_id = %s::uuid",
            (tenant_id, rule_id),
        )

    await in_tenant(pool, tenant_id, _q)
