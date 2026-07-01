"""DB rule-metadata overlay + refresh loop (WP02 — Component 2/3 amendment).

At startup (lifespan) — and again on a refresh loop (``RULES_REFRESH_INTERVAL_SECONDS``,
default 60s) — the overlay reads the platform rows of the ``guardrails.rules`` registry
(``tenant_id IS NULL``) and writes the MUTABLE metadata (``default_action``,
``default_fail_mode``, ``severity``, ``timeout_ms``, ``status``) onto the in-code
:class:`RuleSpec` objects. The DB is authoritative for metadata; code remains
authoritative for ``detect()`` logic. A DB ``status='retired'`` disables the rule (the
pipeline skips it).

Mismatch semantics (readiness-gating): if a code rule_id is missing from the DB
registry, or the DB carries a platform rule_id the build does not know, the overlay
logs both sides and reports ``status='mismatch'`` — which FAILS ``/readyz`` (the
registry and the running build disagree: policies may reference rules the runtime
cannot evaluate, or the runtime enforces rules the registry never blessed).

If the registry cannot be read at all the in-code defaults stand (documented
last-resort fallback) and the overlay reports ``status='unavailable'`` — soft, because
the ``postgresql`` readiness check already owns DB-down.

────────────────────────────────────────────────────────────────────────────────────
WP07 — DYNAMIC CUSTOM-RULE LOADER (:class:`CustomRuleLoader`)
────────────────────────────────────────────────────────────────────────────────────
The built-in 11 rules are compiled in. Tenant CUSTOM rules (regex / classifier-threshold)
live as rows in ``guardrails.rules`` with a non-NULL ``tenant_id`` and an executable
definition (``services.rules.custom``). The loader turns those rows into executable
:class:`RuleSpec` objects and makes them visible to the EXISTING pipeline WITHOUT editing
``pipeline.py``:

  * The pipeline evaluates ``RULES_BY_ID[rid]`` for each ``rid`` in the effective policy's
    ``rules``. The loader REGISTERS each tenant custom spec into the SAME ``RULES_BY_ID``
    dict object the pipeline imported (it mutates the dict; it never rebinds the name), so
    a lookup of a custom rule_id resolves to the executable spec.
  * The custom rule_ids must ALSO appear in the effective policy. The loader exposes
    :meth:`CustomRuleLoader.effective_rule_ids` (and the convenience
    :meth:`with_custom_rules`) for that. THE ONE-LINE PIPELINE-FACING HOOK the parent must
    add lives in ``api/check.py`` (NOT pipeline.py) right after the policy resolves::

        policy = await loader.with_custom_rules(policy, principal.tenant_id)

    where ``loader = request.app.state.custom_rule_loader``. That single call (a) ensures
    the tenant's active custom specs are registered in ``RULES_BY_ID`` and (b) returns the
    policy with the custom rule_ids appended to ``policy.rules`` so the unmodified pipeline
    iterates them. With no loader/pool wired (local/unit) it is a clean no-op passthrough.

Custom specs are cached per tenant for ``custom_rules_cache_ttl_seconds`` (a freshly saved
rule takes effect within that window) and the read is RLS-scoped via ``in_tenant`` — a DB
error fails SOFT (the tenant simply has no custom rules for that request; built-ins stand).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

from ...db.pool import in_tenant
from .custom import CustomRuleRow, build_custom_spec
from .definitions import ALL_RULES, RULES_BY_ID, RuleSpec

logger = structlog.get_logger(__name__)

# Readiness-check values reported under checks['rules_registry'].
STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"  # the ONLY status that fails /readyz
STATUS_UNAVAILABLE = "unavailable"


class RuleRegistryOverlay:
    """Overlays guardrails.rules metadata onto the in-code RuleSpec objects."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        refresh_interval_seconds: float = 60.0,
    ) -> None:
        self._pool = pool
        self._refresh_interval = refresh_interval_seconds
        self._status = STATUS_OK
        self._missing_in_db: tuple[str, ...] = ()
        self._unknown_in_db: tuple[str, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def status(self) -> str:
        """Current readiness value: 'ok' | 'mismatch' | 'unavailable'."""
        return self._status

    @property
    def missing_in_db(self) -> tuple[str, ...]:
        """Code rule_ids absent from the DB registry (mismatch detail)."""
        return self._missing_in_db

    @property
    def unknown_in_db(self) -> tuple[str, ...]:
        """DB platform rule_ids unknown to this build (mismatch detail)."""
        return self._unknown_in_db

    async def load_once(self) -> str:
        """Read the registry and overlay; never raises (fail-soft). Returns the status."""
        if self._pool is None:
            # Local/unit path: no DB configured — the in-code metadata stands alone.
            self._status = STATUS_OK
            return self._status
        try:
            rows = await self._fetch()
        except Exception as exc:  # noqa: BLE001 — registry read is fail-soft (code defaults stand)
            logger.warning("rules_registry_load_failed", error=str(exc))
            self._status = STATUS_UNAVAILABLE
            return self._status
        self._overlay(rows)
        return self._status

    async def start(self) -> None:
        """Start the background refresh loop (call after the initial ``load_once``)."""
        self._task = asyncio.create_task(self._run(), name="rules-registry-refresh")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task

    async def _run(self) -> None:
        # Wait first: the lifespan already did the initial load before readiness.
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._refresh_interval)
            if self._stopping.is_set():
                return
            try:
                await self.load_once()
            except Exception as exc:  # noqa: BLE001 — the loop must keep running
                logger.warning("rules_registry_refresh_error", error=str(exc))

    async def _fetch(self) -> list[tuple[str, str, str, str, int, str]]:
        pool = self._pool
        assert pool is not None
        async with pool.connection(timeout=2.0) as conn:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT rule_id, default_action, default_fail_mode, default_severity,
                       timeout_ms, status
                  FROM guardrails.rules
                 WHERE tenant_id IS NULL
                """
            )
            return await cur.fetchall()

    def _overlay(self, rows: list[tuple[str, str, str, str, int, str]]) -> None:
        db_meta = {row[0]: row for row in rows}
        # The mismatch comparison uses the IMMUTABLE built-in PLATFORM rule set (ALL_RULES),
        # NOT the live ``RULES_BY_ID`` dict. The custom-rule loader REGISTERS tenant rules
        # (tenant_id IS NOT NULL) into ``RULES_BY_ID`` at request time; comparing the mutated
        # dict against the platform-only DB rows (the ``_fetch`` query filters tenant_id IS
        # NULL) would flag every loaded tenant custom rule as "missing_in_db" and spuriously
        # flip /readyz to 'mismatch'. The built-in 11 are the only rules the DB platform
        # registry is expected to mirror, so compare against exactly those.
        code_ids = {spec.rule_id for spec in ALL_RULES}
        db_ids = set(db_meta)

        # DB is authoritative for metadata on every rule both sides know about.
        for rule_id in code_ids & db_ids:
            _, default_action, default_fail_mode, default_severity, timeout_ms, status = (
                db_meta[rule_id]
            )
            spec = RULES_BY_ID[rule_id]
            spec.default_action = default_action
            spec.default_fail_mode = default_fail_mode
            spec.severity = default_severity
            spec.timeout_ms = timeout_ms
            spec.status = status

        self._missing_in_db = tuple(sorted(code_ids - db_ids))
        self._unknown_in_db = tuple(sorted(db_ids - code_ids))
        if self._missing_in_db or self._unknown_in_db:
            self._status = STATUS_MISMATCH
            logger.warning(
                "rules_registry_mismatch",
                missing_in_db=list(self._missing_in_db),
                unknown_in_db=list(self._unknown_in_db),
            )
        else:
            self._status = STATUS_OK


# ── WP07: dynamic per-tenant custom-rule loader ───────────────────────────────────


# Custom-rule statuses we exclude from execution (matches definitions.RULE_STATUS_RETIRED).
_LOADED_EXCLUDED_STATUSES = ("retired",)


class CustomRuleLoader:
    """Loads tenant CUSTOM rules from the DB into the live ``RULES_BY_ID`` registry.

    Per-tenant cache: a tenant's active custom :class:`RuleSpec` objects are cached for
    ``ttl_seconds``; within the window the check path reuses them without a DB read. The
    read is RLS-scoped (``in_tenant``) and fails SOFT — on any error the tenant simply has
    no custom rules for that request and the built-ins stand. With ``pool is None``
    (local/unit) every call is a clean no-op passthrough.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        ttl_seconds: float = 30.0,
    ) -> None:
        self._pool = pool
        self._ttl = ttl_seconds
        # tenant_id -> (expires_at_monotonic, rule_ids tuple)
        self._cache: dict[str, tuple[float, tuple[str, ...]]] = {}

    async def load_for_tenant(self, tenant_id: str) -> tuple[str, ...]:
        """Return the tenant's active custom rule_ids, registering their specs in RULES_BY_ID.

        Cached for ``ttl_seconds`` per tenant. Always returns a tuple (empty when there are
        no custom rules, no pool, or the read failed) — never raises on the check path.
        """
        if self._pool is None:
            return ()
        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            rows = await self._fetch(tenant_id)
        except Exception as exc:  # noqa: BLE001 — custom-rule load is fail-soft on the check path
            logger.warning("custom_rules_load_failed", tenant_id=tenant_id, error=str(exc))
            # Serve a stale entry if we have one; otherwise no custom rules this request.
            return cached[1] if cached is not None else ()

        rule_ids = self._register(rows)
        self._cache[tenant_id] = (now + self._ttl, rule_ids)
        return rule_ids

    def invalidate(self, tenant_id: str) -> None:
        """Drop a tenant's cache entry so the next request re-reads (e.g. after a CRUD write)."""
        self._cache.pop(tenant_id, None)

    async def with_custom_rules[P](self, policy: P, tenant_id: str) -> P:
        """Return ``policy`` with the tenant's active custom rule_ids appended to ``rules``.

        THE one-line hook ``api/check.py`` calls after resolving the effective policy. Loads
        + registers the tenant's custom specs, then appends any not already present (so a
        policy that explicitly references a custom rule keeps its action_override). A
        no-op passthrough when the tenant has no custom rules / no pool is wired.
        """
        rule_ids = await self.load_for_tenant(tenant_id)
        if not rule_ids:
            return policy
        # Structural use of EffectivePolicy/EnabledRule; imported lazily to avoid a
        # circular import (policy_engine imports services.rules).
        from ..policy_engine import EffectivePolicy, EnabledRule

        existing = {er.rule_id for er in policy.rules}  # type: ignore[attr-defined]
        extra = tuple(EnabledRule(rid) for rid in rule_ids if rid not in existing)
        if not extra:
            return policy
        merged = EffectivePolicy(
            policy_id=policy.policy_id,  # type: ignore[attr-defined]
            name=policy.name,  # type: ignore[attr-defined]
            rules=(*policy.rules, *extra),  # type: ignore[attr-defined]
        )
        return merged  # type: ignore[return-value]

    async def _fetch(self, tenant_id: str) -> list[CustomRuleRow]:
        pool = self._pool
        assert pool is not None

        async def _txn(conn: object) -> list[CustomRuleRow]:
            cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
                """
                SELECT rule_id, tenant_id::text, version, name, custom_type, direction,
                       default_action, default_fail_mode, default_severity, default_category,
                       timeout_ms, status, pattern, classifier_category, threshold
                  FROM guardrails.rules
                 WHERE tenant_id = %s
                   AND custom_type IS NOT NULL
                   AND status <> ALL(%s)
                """,
                (tenant_id, list(_LOADED_EXCLUDED_STATUSES)),
            )
            out: list[CustomRuleRow] = []
            for r in await cur.fetchall():
                out.append(
                    CustomRuleRow(
                        rule_id=r[0],
                        tenant_id=r[1],
                        version=int(r[2]) if str(r[2]).isdigit() else 1,
                        name=r[3] or r[0],
                        custom_type=r[4],
                        direction=r[5],
                        default_action=r[6],
                        default_fail_mode=r[7],
                        severity=r[8],
                        category=r[9],
                        timeout_ms=r[10],
                        status=r[11],
                        pattern=r[12],
                        classifier_category=r[13],
                        threshold=float(r[14]) if r[14] is not None else None,
                    )
                )
            return out

        return await in_tenant(pool, tenant_id, _txn)

    def _register(self, rows: list[CustomRuleRow]) -> tuple[str, ...]:
        """Build specs from rows + write them into the shared RULES_BY_ID dict.

        Returns the rule_ids successfully registered (order-stable, lexicographic).
        """
        rule_ids: list[str] = []
        for row in rows:
            spec: RuleSpec | None = build_custom_spec(row)
            if spec is None:
                continue
            RULES_BY_ID[spec.rule_id] = spec
            rule_ids.append(spec.rule_id)
        return tuple(sorted(rule_ids))
