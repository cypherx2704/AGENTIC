"""Policy resolution (Component 3).

Resolves the EFFECTIVE policy for a check via the chain:

  1. ``agent_policies`` row for (agent_id, tenant_id) -> that policy.
  2. Tenant default: ``policies WHERE tenant_id = $1 AND is_default AND status='active'``.
  3. Platform default: ``policies WHERE tenant_id IS NULL AND is_default AND status='active'``.

If the DB is unreachable (or no row resolves) we fall back to the built-in platform
default that enables all 11 first-cycle rules — so the critical path runs without a
populated DB in local/dev. The Valkey policy cache is intentionally out of scope for
first cycle (a DB read is fine; see phase doc Component 3 / Audit #6).

A resolved policy is a list of :class:`EnabledRule` entries: a rule_id plus an optional
``action_override`` (null = use the rule's ``default_action``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..db.outbox import record_policy_change
from ..db.pool import in_tenant
from .rules import ALL_RULES, RULE_STATUS_RETIRED, RULES_BY_ID

logger = structlog.get_logger(__name__)

# Deterministic UUID for the seeded built-in platform default policy. The seed
# migration inserts a row with this id so DB-resolved and built-in policies agree.
PLATFORM_DEFAULT_POLICY_ID = "00000000-0000-0000-0000-0000000d0001"
PLATFORM_DEFAULT_POLICY_NAME = "Platform Default Policy"

# Valid enum values for save-time validation (mirrors the rules/DB enums).
VALID_ACTIONS = frozenset({"allow", "warn", "redact", "block"})
VALID_STREAM_MODES = frozenset({"buffer"})  # 'buffer' is the only first-cycle mode
VALID_FAIL_MODES = frozenset({"open", "closed"})
_POLICY_STATUS_ACTIVE = "active"
_POLICY_STATUS_SUPERSEDED = "superseded"


class PolicyValidationError(Exception):
    """A draft policy failed save-time validation; carries per-issue details.

    The API layer renders this as a 422 VALIDATION_ERROR with ``details=issues``.
    """

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__("policy validation failed")
        self.issues = issues


class PolicyNotFoundError(Exception):
    """No policy (root) resolves for the given id within the tenant scope."""


class PlatformPolicyImmutableError(Exception):
    """A tenant attempted to edit/assign a platform (tenant_id IS NULL) policy."""


def validate_draft(
    *,
    name: str | None,
    rules: object,
    stream_mode: str,
    fail_mode_override: str | None,
) -> list[dict[str, Any]]:
    """Save-time validation: return a list of issues (empty == valid).

    Checks: name present; stream_mode valid; fail_mode_override valid (if set); rules is a
    non-empty list whose every referenced rule_id exists in the registry, is not retired,
    and whose action_override (when present) is a valid action.
    """
    issues: list[dict[str, Any]] = []
    if not name or not isinstance(name, str) or not name.strip():
        issues.append({"field": "name", "reason": "required"})
    if stream_mode not in VALID_STREAM_MODES:
        issues.append({"field": "stream_mode", "reason": "invalid", "value": stream_mode})
    if fail_mode_override is not None and fail_mode_override not in VALID_FAIL_MODES:
        issues.append(
            {"field": "fail_mode_override", "reason": "invalid", "value": fail_mode_override}
        )
    if not isinstance(rules, list) or not rules:
        issues.append({"field": "rules", "reason": "must_be_non_empty_list"})
        return issues
    for i, entry in enumerate(rules):
        if not isinstance(entry, dict):
            issues.append({"field": f"rules[{i}]", "reason": "must_be_object"})
            continue
        rule_id = entry.get("rule_id")
        if not isinstance(rule_id, str) or rule_id not in RULES_BY_ID:
            issues.append({"field": f"rules[{i}].rule_id", "reason": "unknown_rule", "value": rule_id})
            continue
        if RULES_BY_ID[rule_id].status == RULE_STATUS_RETIRED:
            issues.append({"field": f"rules[{i}].rule_id", "reason": "retired_rule", "value": rule_id})
        override = entry.get("action_override")
        if override is not None and override not in VALID_ACTIONS:
            issues.append(
                {"field": f"rules[{i}].action_override", "reason": "invalid_action", "value": override}
            )
    return issues


def _normalize_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize a validated rules array for storage (stable key set)."""
    out: list[dict[str, Any]] = []
    for entry in rules:
        out.append(
            {
                "rule_id": entry["rule_id"],
                "enabled": entry.get("enabled", True) is not False,
                "action_override": entry.get("action_override"),
            }
        )
    return out


@dataclass(frozen=True)
class EnabledRule:
    """A rule enabled by a policy, with an optional action override."""

    rule_id: str
    action_override: str | None = None


@dataclass(frozen=True)
class EffectivePolicy:
    """The resolved policy applied to a check.

    ``fail_mode_override`` (when set) overrides each rule's ``default_fail_mode`` for the
    whole policy — the check path ignores it today (it stays on the rule default), but the
    pipeline trace mode reports it so authors can see the effective fail posture.
    """

    policy_id: str
    name: str
    rules: tuple[EnabledRule, ...]
    fail_mode_override: str | None = None


def builtin_platform_default() -> EffectivePolicy:
    """Built-in platform-default policy enabling all 11 rules at their default action."""
    return EffectivePolicy(
        policy_id=PLATFORM_DEFAULT_POLICY_ID,
        name=PLATFORM_DEFAULT_POLICY_NAME,
        rules=tuple(EnabledRule(r.rule_id) for r in ALL_RULES),
    )


def _parse_rules_json(rules_json: object) -> tuple[EnabledRule, ...]:
    """Parse the policies.rules JSONB array into EnabledRule entries."""
    out: list[EnabledRule] = []
    if isinstance(rules_json, list):
        for entry in rules_json:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            rule_id = entry.get("rule_id")
            if not isinstance(rule_id, str):
                continue
            override = entry.get("action_override")
            out.append(EnabledRule(rule_id, override if isinstance(override, str) else None))
    return tuple(out)


class PolicyEngine:
    """Resolves the effective policy for a (tenant, agent) pair from the DB."""

    def __init__(self, pool: AsyncConnectionPool | None) -> None:
        self._pool = pool

    async def resolve(self, tenant_id: str, agent_id: str | None) -> EffectivePolicy:
        """Return the effective policy, falling back to the built-in default on any miss."""
        if self._pool is None:
            return builtin_platform_default()
        try:
            return await self._resolve_from_db(tenant_id, agent_id)
        except Exception as exc:  # noqa: BLE001 — never fail the check path on DB hiccups
            logger.warning("policy_resolve_fallback", error=str(exc))
            return builtin_platform_default()

    async def _resolve_from_db(self, tenant_id: str, agent_id: str | None) -> EffectivePolicy:
        pool = self._pool
        assert pool is not None

        async def _txn(conn: object) -> EffectivePolicy | None:
            # 1) agent-assigned policy. agent_policies.policy_id stores the ROOT id; we
            #    resolve to the ACTIVE version of that chain so an append-only edit
            #    auto-flips assigned agents. A pre-WP07 row that pointed at a concrete
            #    (self-rooted, active) version still matches.
            if agent_id is not None:
                cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
                    """
                    SELECT p.policy_id::text, p.name, p.rules, p.fail_mode_override
                      FROM guardrails.agent_policies ap
                      JOIN guardrails.policies p
                        ON p.root_policy_id = ap.policy_id AND p.status = 'active'
                     WHERE ap.agent_id = %s AND ap.tenant_id = %s
                    """,
                    (agent_id, tenant_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    return EffectivePolicy(row[0], row[1], _parse_rules_json(row[2]), row[3])

            # 2) tenant default, then 3) platform default (tenant_id IS NULL)
            cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
                """
                SELECT policy_id::text, name, rules, fail_mode_override, tenant_id IS NULL AS is_platform
                  FROM guardrails.policies
                 WHERE is_default = true AND status = 'active'
                   AND (tenant_id = %s OR tenant_id IS NULL)
                 ORDER BY is_platform ASC
                 LIMIT 1
                """,
                (tenant_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return EffectivePolicy(row[0], row[1], _parse_rules_json(row[2]), row[3])
            return None

        resolved = await in_tenant(pool, tenant_id, _txn)
        if resolved is None:
            # No platform default in DB — fall back to built-in (keeps the path alive).
            logger.warning("no_platform_default_policy")
            return builtin_platform_default()
        return resolved

    async def has_platform_default(self) -> bool:
        """Readiness probe helper: True if a platform-default policy exists.

        Best-effort: a DB error returns False so readiness reflects DB state. With no
        pool configured (local/unit), the built-in default stands in, so returns True.
        """
        if self._pool is None:
            return True
        try:
            async with self._pool.connection(timeout=2.0) as conn:
                cur = await conn.cursor(row_factory=tuple_row).execute(
                    """
                    SELECT 1 FROM guardrails.policies
                     WHERE tenant_id IS NULL AND is_default = true AND status = 'active'
                     LIMIT 1
                    """
                )
                return await cur.fetchone() is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("platform_default_check_failed", error=str(exc))
            return False

    # ── Policy CRUD (WP07) — append-only version chain + atomic agent repoint ────────
    #
    # All writes run inside ``in_tenant`` so RLS scopes them to the caller's tenant: a
    # tenant can only ever insert/update its OWN rows (WITH CHECK tenant_id = app.tenant_id),
    # so platform (tenant_id IS NULL) policies are immutable from the API. Reads admit the
    # tenant's own rows plus platform defaults (p_policies_read).

    def _require_pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise PolicyNotFoundError("no database configured")
        return self._pool

    async def create_policy(
        self,
        *,
        tenant_id: str,
        name: str,
        rules: list[dict[str, Any]],
        stream_mode: str,
        fail_mode_override: str | None,
        actor_agent_id: str | None,
        request_id: str | None,
        trace_id: str | None,
        producer_version: str,
    ) -> dict[str, Any]:
        """Create a brand-new logical policy (version 1, its own root). Returns the row dict.

        The caller MUST have validated the draft first (``validate_draft``).
        """
        pool = self._require_pool()
        normalized = _normalize_rules(rules)

        async def _txn(conn: AsyncConnection) -> dict[str, Any]:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                INSERT INTO guardrails.policies
                  (tenant_id, name, version, status, rules, is_default,
                   stream_mode, fail_mode_override, created_by)
                VALUES (%s, %s, 1, 'active', %s, false, %s, %s, %s)
                RETURNING policy_id
                """,
                (tenant_id, name, Jsonb(normalized), stream_mode, fail_mode_override, actor_agent_id),
            )
            new_id = str((await cur.fetchone())["policy_id"])  # type: ignore[index]
            # A v1 row is its own root.
            await conn.execute(
                "UPDATE guardrails.policies SET root_policy_id = policy_id WHERE policy_id = %s",
                (new_id,),
            )
            await _insert_audit(
                conn,
                tenant_id=tenant_id,
                root_policy_id=new_id,
                policy_id=new_id,
                action="created",
                actor_agent_id=actor_agent_id,
                request_id=request_id,
                trace_id=trace_id,
                details={"name": name, "stream_mode": stream_mode,
                         "fail_mode_override": fail_mode_override},
            )
            await record_policy_change(
                conn,
                tenant_id=tenant_id,
                trace_id=trace_id or "",
                action="created",
                root_policy_id=new_id,
                policy_id=new_id,
                details={"name": name, "fail_mode_override": fail_mode_override},
                producer_version=producer_version,
            )
            return await _fetch_version_row(conn, new_id)

        return await in_tenant(pool, tenant_id, _txn)

    async def edit_policy(
        self,
        *,
        tenant_id: str,
        root_policy_id: str,
        name: str,
        rules: list[dict[str, Any]],
        stream_mode: str,
        fail_mode_override: str | None,
        actor_agent_id: str | None,
        request_id: str | None,
        trace_id: str | None,
        producer_version: str,
    ) -> dict[str, Any]:
        """Append-only edit: insert a NEW version and atomically repoint the active pointer.

        Never mutates the published rules of an existing version. The currently-active
        version is set to 'superseded' and the new row to 'active' inside ONE transaction,
        so the ``uq_policies_active_per_root`` index guarantees a single active version.
        """
        pool = self._require_pool()
        normalized = _normalize_rules(rules)

        async def _txn(conn: AsyncConnection) -> dict[str, Any]:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT policy_id, version, tenant_id, is_default, fail_mode_override
                  FROM guardrails.policies
                 WHERE root_policy_id = %s AND status = 'active'
                 FOR UPDATE
                """,
                (root_policy_id,),
            )
            current = await cur.fetchone()
            if current is None:
                raise PolicyNotFoundError(root_policy_id)
            if current["tenant_id"] is None:
                # Platform policy — RLS would block the write anyway; fail explicitly.
                raise PlatformPolicyImmutableError(root_policy_id)

            old_fail_mode = current["fail_mode_override"]
            # 1) supersede the current active version (no rules mutation).
            await conn.execute(
                "UPDATE guardrails.policies SET status = 'superseded', updated_at = NOW() "
                "WHERE policy_id = %s",
                (current["policy_id"],),
            )
            # 2) insert the new active version, carrying root + chain link.
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                INSERT INTO guardrails.policies
                  (tenant_id, name, version, status, rules, is_default,
                   previous_policy_id, root_policy_id, stream_mode, fail_mode_override, created_by)
                VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)
                RETURNING policy_id
                """,
                (
                    tenant_id, name, current["version"] + 1, Jsonb(normalized),
                    current["is_default"], current["policy_id"], root_policy_id,
                    stream_mode, fail_mode_override, actor_agent_id,
                ),
            )
            new_id = str((await cur.fetchone())["policy_id"])  # type: ignore[index]

            await _insert_audit(
                conn,
                tenant_id=tenant_id,
                root_policy_id=root_policy_id,
                policy_id=new_id,
                action="edited",
                actor_agent_id=actor_agent_id,
                request_id=request_id,
                trace_id=trace_id,
                details={"version": current["version"] + 1},
            )
            # fail_mode_override changes are explicitly AUDITED (own audit row + bus event).
            if fail_mode_override != old_fail_mode:
                await _insert_audit(
                    conn,
                    tenant_id=tenant_id,
                    root_policy_id=root_policy_id,
                    policy_id=new_id,
                    action="fail_mode_override_changed",
                    actor_agent_id=actor_agent_id,
                    request_id=request_id,
                    trace_id=trace_id,
                    details={"old": old_fail_mode, "new": fail_mode_override},
                )
                await record_policy_change(
                    conn,
                    tenant_id=tenant_id,
                    trace_id=trace_id or "",
                    action="fail_mode_override_changed",
                    root_policy_id=root_policy_id,
                    policy_id=new_id,
                    details={"old": old_fail_mode, "new": fail_mode_override},
                    producer_version=producer_version,
                )
            await record_policy_change(
                conn,
                tenant_id=tenant_id,
                trace_id=trace_id or "",
                action="edited",
                root_policy_id=root_policy_id,
                policy_id=new_id,
                details={"version": current["version"] + 1},
                producer_version=producer_version,
            )
            return await _fetch_version_row(conn, new_id)

        return await in_tenant(pool, tenant_id, _txn)

    async def get_policy(self, *, tenant_id: str, root_policy_id: str) -> dict[str, Any]:
        """Return the active version + the full version chain (newest first)."""
        pool = self._require_pool()

        async def _txn(conn: AsyncConnection) -> dict[str, Any]:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT policy_id::text, root_policy_id::text, tenant_id::text, name, version,
                       status, rules, is_default, stream_mode, fail_mode_override,
                       previous_policy_id::text, created_at, updated_at
                  FROM guardrails.policies
                 WHERE root_policy_id = %s
                 ORDER BY version DESC
                """,
                (root_policy_id,),
            )
            rows = await cur.fetchall()
            if not rows:
                raise PolicyNotFoundError(root_policy_id)
            active = next((r for r in rows if r["status"] == _POLICY_STATUS_ACTIVE), rows[0])
            return {"active": _row_to_dict(active), "versions": [_row_to_dict(r) for r in rows]}

        return await in_tenant(pool, tenant_id, _txn)

    async def assign_policy(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        root_policy_id: str,
        actor_agent_id: str | None,
        request_id: str | None,
        trace_id: str | None,
        producer_version: str,
    ) -> dict[str, Any]:
        """Atomically (re)point an agent at a policy ROOT (UPSERT). Audited."""
        pool = self._require_pool()

        async def _txn(conn: AsyncConnection) -> dict[str, Any]:
            # The policy root must exist + be the tenant's own active policy (RLS scopes read).
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT tenant_id, policy_id::text
                  FROM guardrails.policies
                 WHERE root_policy_id = %s AND status = 'active'
                """,
                (root_policy_id,),
            )
            target = await cur.fetchone()
            if target is None:
                raise PolicyNotFoundError(root_policy_id)
            if target["tenant_id"] is None:
                raise PlatformPolicyImmutableError(root_policy_id)
            # Atomic repoint: a single UPSERT flips the agent's active policy pointer.
            await conn.execute(
                """
                INSERT INTO guardrails.agent_policies (agent_id, tenant_id, policy_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (agent_id, tenant_id)
                DO UPDATE SET policy_id = EXCLUDED.policy_id
                """,
                (agent_id, tenant_id, root_policy_id),
            )
            await _insert_audit(
                conn,
                tenant_id=tenant_id,
                root_policy_id=root_policy_id,
                policy_id=target["policy_id"],
                action="assigned",
                actor_agent_id=actor_agent_id,
                request_id=request_id,
                trace_id=trace_id,
                details={"agent_id": agent_id},
            )
            await record_policy_change(
                conn,
                tenant_id=tenant_id,
                trace_id=trace_id or "",
                action="assigned",
                root_policy_id=root_policy_id,
                policy_id=target["policy_id"],
                details={"agent_id": agent_id},
                producer_version=producer_version,
            )
            return {"agent_id": agent_id, "policy_id": root_policy_id}

        return await in_tenant(pool, tenant_id, _txn)

    async def resolve_for_simulation(
        self,
        *,
        tenant_id: str,
        root_policy_id: str,
        as_of: str | None = None,
    ) -> EffectivePolicy:
        """Resolve a stored policy to an :class:`EffectivePolicy` for simulation.

        ``as_of`` (ISO-8601 timestamp) resolves the version that was ACTIVE at that time:
        the version with the greatest ``created_at <= as_of`` in the chain. With no
        ``as_of`` the current active version is used. Raises :class:`PolicyNotFoundError`
        when nothing resolves.
        """
        pool = self._require_pool()

        async def _txn(conn: AsyncConnection) -> EffectivePolicy:
            if as_of is None:
                cur = await conn.cursor(row_factory=tuple_row).execute(
                    """
                    SELECT policy_id::text, name, rules, fail_mode_override
                      FROM guardrails.policies
                     WHERE root_policy_id = %s AND status = 'active'
                    """,
                    (root_policy_id,),
                )
            else:
                cur = await conn.cursor(row_factory=tuple_row).execute(
                    """
                    SELECT policy_id::text, name, rules, fail_mode_override
                      FROM guardrails.policies
                     WHERE root_policy_id = %s AND created_at <= %s
                     ORDER BY version DESC
                     LIMIT 1
                    """,
                    (root_policy_id, as_of),
                )
            row = await cur.fetchone()
            if row is None:
                raise PolicyNotFoundError(root_policy_id)
            return EffectivePolicy(row[0], row[1], _parse_rules_json(row[2]), row[3])

        return await in_tenant(pool, tenant_id, _txn)


async def _insert_audit(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    root_policy_id: str,
    policy_id: str,
    action: str,
    actor_agent_id: str | None,
    request_id: str | None,
    trace_id: str | None,
    details: dict[str, Any],
) -> None:
    """Append one redaction-safe row to guardrails.policy_audit (same txn as the change)."""
    await conn.execute(
        """
        INSERT INTO guardrails.policy_audit
          (tenant_id, root_policy_id, policy_id, action, actor_agent_id,
           request_id, trace_id, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (tenant_id, root_policy_id, policy_id, action, actor_agent_id,
         request_id, trace_id, Jsonb(details)),
    )


async def _fetch_version_row(conn: AsyncConnection, policy_id: str) -> dict[str, Any]:
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT policy_id::text, root_policy_id::text, tenant_id::text, name, version,
               status, rules, is_default, stream_mode, fail_mode_override,
               previous_policy_id::text, created_at, updated_at
          FROM guardrails.policies
         WHERE policy_id = %s
        """,
        (policy_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return _row_to_dict(row)


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a policies row for the API (public id = root_policy_id; ISO timestamps)."""
    created = row.get("created_at")
    updated = row.get("updated_at")
    return {
        "policy_id": row["root_policy_id"],   # PUBLIC id is the stable root
        "version_id": row["policy_id"],       # the concrete version's own id
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "rules": row["rules"],
        "is_default": row["is_default"],
        "stream_mode": row["stream_mode"],
        "fail_mode_override": row["fail_mode_override"],
        "previous_policy_id": row["previous_policy_id"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def new_check_id() -> str:
    """Return a fresh check_id (UUIDv4)."""
    return str(uuid.uuid4())
