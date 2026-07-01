"""Deterministic PII redaction + per-tenant key lifecycle (Component 5 / WP07).

PII matches are replaced with a deterministic token::

    [REDACTED:<category>:<token>]

where ``token`` = first 8 hex chars of ``HMAC-SHA256(key, tenant_id + ":" + matched_text)``.

Properties (Component 5):
  * Same value + same tenant + same key  → same token (downstream LLM treats repeated
    occurrences as the same entity).
  * Same value + different tenant         → different token (no cross-tenant linkability).
  * Same value + after key rotation       → different token (rotation invalidates the link).
  * Without the HMAC key the token is not invertible.

Per-tenant key resolution (WP07): the effective key for a tenant is the ``current``
(post-rotation: also still-in-grace ``retired``) row in ``guardrails.tenant_redaction_keys``,
whose ``key_ref`` is resolved to raw key material via a PLUGGABLE scheme:

  * ``env:NAME``          → environment variable NAME (first-cycle / dev BYO key).
  * ``env:``              → the platform key (``settings.redaction_hmac_key_platform``).
  * ``sealed:<blob>``     → unsealed via the platform secret store (AWS Secrets Manager /
                            SOPS in prod). Not wired first cycle.
  * ``secretsmanager:<arn>`` → legacy alias for ``sealed:`` (same prod path).

If there is no DB pool, no row, or the ref cannot be resolved, the resolver FALLS BACK to
the per-env platform key — so existing keyless behaviour holds and a check never fails on
a key-resolution problem. Resolved keys are cached per tenant for a short TTL so the hot
path does not read the DB on every check.

Rotation (POST /v1/redaction-keys/rotate, see api/redaction_keys.py): mints a new
``current`` row and demotes the prior ``current`` to ``retired`` with a ``retired_at`` of
NOW; the retired key stays VALID for ``redaction_key_grace_days`` so tokens minted just
before rotation still resolve. A lifespan-scheduled :class:`RedactionKeyRetirementJob`
hard-retires (clears) keys past grace.
"""

from __future__ import annotations

import hmac
import os
import time
from hashlib import sha256

import structlog
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)

# Categories that are PII and therefore redacted to a token (Component 4/5).
PII_CATEGORIES: frozenset[str] = frozenset(
    {"pii", "email", "phone", "credit_card", "ssn", "name", "passport", "ip", "address"}
)

# Accepted key_ref schemes (the legacy 'secretsmanager:' CHECK constraint maps to sealed).
_SCHEME_ENV = "env:"
_SCHEME_SEALED = "sealed:"
_SCHEME_SECRETSMANAGER = "secretsmanager:"


def compute_token(key: str, tenant_id: str, matched_text: str) -> str:
    """Return the first 8 hex chars of HMAC-SHA256(key, tenant_id + ':' + matched_text)."""
    data = f"{tenant_id}:{matched_text}".encode()
    digest = hmac.new(key.encode("utf-8"), data, sha256).hexdigest()
    return digest[:8]


def redaction_token(key: str, category: str, tenant_id: str, matched_text: str) -> str:
    """Return the full ``[REDACTED:<category>:<token>]`` placeholder for a match."""
    token = compute_token(key, tenant_id, matched_text)
    return f"[REDACTED:{category}:{token}]"


def resolve_key_ref(key_ref: str, *, platform_key: str) -> str | None:
    """Resolve a pluggable ``key_ref`` to raw key material; ``None`` if unresolvable.

    Pure + side-effect-free except for reading the process environment (``env:`` scheme).
    Unknown schemes / unset env vars / not-yet-wired sealed refs return ``None`` so the
    caller falls back to the platform key (fail-soft).
    """
    if key_ref.startswith(_SCHEME_ENV):
        name = key_ref[len(_SCHEME_ENV):]
        if not name:
            return platform_key  # bare 'env:' => platform key
        return os.environ.get(name)
    if key_ref.startswith(_SCHEME_SEALED) or key_ref.startswith(_SCHEME_SECRETSMANAGER):
        # Prod: unseal via AWS Secrets Manager / SOPS. Not wired first cycle — fall back.
        logger.info("redaction_key_ref_sealed_unresolved", scheme=key_ref.split(":", 1)[0])
        return None
    logger.warning("redaction_key_ref_unknown_scheme", key_ref=key_ref[:16])
    return None


class RedactionKeyResolver:
    """Resolves the effective HMAC key for a tenant (DB-backed override, else platform).

    Synchronous :meth:`resolve` serves the hot path from an in-memory cache; the cache is
    populated by :meth:`refresh_tenant` (an async DB read-through, called by the check
    handler before evaluation). With no pool / no row / unresolvable ref the platform key
    is returned, so behaviour is unchanged when no per-tenant key exists.
    """

    def __init__(
        self,
        platform_key: str,
        *,
        pool: AsyncConnectionPool | None = None,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._platform_key = platform_key
        self._pool = pool
        self._cache_ttl = cache_ttl_seconds
        # tenant_id -> resolved key. Explicitly registered keys (tests / loaders) never expire.
        self._tenant_keys: dict[str, str] = {}
        # tenant_id -> monotonic expiry for DB-read-through entries.
        self._cache_expiry: dict[str, float] = {}
        # tenant_id -> monotonic expiry for a NEGATIVE result ("no override; use platform"),
        # so a keyless tenant does not read the DB on every check. A key added later is
        # picked up once the negative entry expires.
        self._negative_until: dict[str, float] = {}

    def register_tenant_key(self, tenant_id: str, key: str) -> None:
        """Inject a resolved per-tenant key (used by loaders / tests). Never expires."""
        self._tenant_keys[tenant_id] = key
        self._cache_expiry.pop(tenant_id, None)
        self._negative_until.pop(tenant_id, None)

    def invalidate(self, tenant_id: str) -> None:
        """Evict all cached state for a tenant (called after a key rotation)."""
        self._tenant_keys.pop(tenant_id, None)
        self._cache_expiry.pop(tenant_id, None)
        self._negative_until.pop(tenant_id, None)

    def resolve(self, tenant_id: str) -> str:
        """Return the effective HMAC key for the tenant (override else platform).

        Synchronous + non-blocking: serves the in-memory cache only. A stale/absent cache
        entry simply yields the platform key (safe fallback) until :meth:`refresh_tenant`
        repopulates it. Expired read-through entries are evicted here.
        """
        expiry = self._cache_expiry.get(tenant_id)
        if expiry is not None and time.monotonic() >= expiry:
            self._tenant_keys.pop(tenant_id, None)
            self._cache_expiry.pop(tenant_id, None)
        return self._tenant_keys.get(tenant_id, self._platform_key)

    async def refresh_tenant(self, tenant_id: str) -> str:
        """Read-through the tenant's current key from the DB into the cache; return it.

        Fail-soft: no pool / no row / DB error / unresolvable ref → platform key. The
        result is cached for ``cache_ttl_seconds`` so subsequent hot-path :meth:`resolve`
        calls are in-memory.
        """
        now = time.monotonic()
        # A still-fresh positive entry (or a permanently registered key) needs no DB hit.
        expiry = self._cache_expiry.get(tenant_id)
        if tenant_id in self._tenant_keys and (expiry is None or now < expiry):
            return self._tenant_keys[tenant_id]
        # A still-fresh NEGATIVE entry: keyless tenant — use platform without a DB read.
        neg = self._negative_until.get(tenant_id)
        if neg is not None and now < neg:
            return self._platform_key
        if self._pool is None:
            return self._platform_key

        key_ref = await self._read_current_key_ref(tenant_id)
        resolved: str | None = None
        if key_ref is not None:
            resolved = resolve_key_ref(key_ref, platform_key=self._platform_key)

        if resolved is not None:
            self._tenant_keys[tenant_id] = resolved
            self._negative_until.pop(tenant_id, None)
            if self._cache_ttl > 0:
                self._cache_expiry[tenant_id] = now + self._cache_ttl
            return resolved
        # No usable override -> platform key, cached negatively for the TTL.
        self._tenant_keys.pop(tenant_id, None)
        self._cache_expiry.pop(tenant_id, None)
        if self._cache_ttl > 0:
            self._negative_until[tenant_id] = now + self._cache_ttl
        return self._platform_key

    async def _read_current_key_ref(self, tenant_id: str) -> str | None:
        """Return the tenant's effective key_ref (current, else newest in-grace retired)."""
        pool = self._pool
        assert pool is not None

        async def _txn(conn: object) -> str | None:
            # Prefer the active 'current' key; if none, the newest still-in-grace 'retired'
            # key (rotation just happened / current was cleared) keeps old tokens resolving.
            cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
                """
                SELECT key_ref
                  FROM guardrails.tenant_redaction_keys
                 WHERE tenant_id = %s
                   AND (status = 'current' OR (status = 'retired' AND retired_at IS NOT NULL))
                 ORDER BY (status = 'current') DESC, created_at DESC
                 LIMIT 1
                """,
                (tenant_id,),
            )
            row = await cur.fetchone()
            return str(row[0]) if row is not None else None

        try:
            return await in_tenant(pool, tenant_id, _txn)
        except Exception as exc:  # noqa: BLE001 — never fail a check on a key-read error
            logger.warning("redaction_key_read_failed", error=str(exc))
            return None
