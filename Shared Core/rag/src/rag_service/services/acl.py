"""KB access control (Component 5c) — per-principal ACL enforcement.

Default: any agent in tenant T can query any KB in T (a default ``(tenant,'*')`` ACL row
is created on KB creation). External SaaS uses ``principal_type='user'`` (or agent/api_key)
rows for finer partition. Enforcement runs on EVERY retrieval / ingest / mgmt call:

  1. Resolve the calling principal's identities from the JWT (agent_id, api_key_id,
     opaque user_id, plus the implicit ``tenant`` identity).
  2. Load ``rag.kb_acls`` rows for the KB matching ANY of those identities (or the
     ``tenant='*'`` wildcard), excluding expired rows.
  3. The required permission for the operation must appear in some matching row's
     ``permissions[]``. Otherwise 403 ``FORBIDDEN_KB``.

A KB with ZERO matching ACL rows is readable by NO ONE (no API-layer fallback — the
platform-skills bootstrap inserts its default ACL in the same txn for exactly this reason).
"""

from __future__ import annotations

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)

# Operation -> required permission (subset of read/query/ingest/write/admin).
OP_QUERY = "query"
OP_INGEST = "ingest"
OP_WRITE = "write"
OP_ADMIN = "admin"


def _principal_identities(principal: Principal) -> list[tuple[str, str]]:
    """All (principal_type, principal_id) tuples this caller can be matched against."""
    ids: list[tuple[str, str]] = [("tenant", "*")]
    if principal.agent_id:
        ids.append(("agent", principal.agent_id))
    if principal.api_key_id:
        ids.append(("api_key", principal.api_key_id))
    if principal.user_id:
        ids.append(("user", principal.user_id))
    return ids


async def check_access(
    pool: AsyncConnectionPool,
    principal: Principal,
    kb_id: str,
    operation: str,
    *,
    settings: Settings,
) -> None:
    """Raise 403 FORBIDDEN_KB unless the principal holds ``operation`` on ``kb_id``.

    When no DB pool is wired (unit tests), access is allowed (the ACL store is the
    authority; absent a store there is nothing to enforce against).
    """
    if pool is None:
        return

    identities = _principal_identities(principal)
    types = [t for t, _ in identities]
    pid_for_type = dict(identities)

    async def _txn(conn: AsyncConnection) -> bool:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT principal_type, principal_id, permissions
              FROM rag.kb_acls
             WHERE kb_id = %s
               AND principal_type = ANY(%s)
               AND (expires_at IS NULL OR expires_at > NOW())
            """,
            (kb_id, types),
        )
        rows = await cur.fetchall()
        for row in rows:
            ptype = row["principal_type"]
            pid = row["principal_id"]
            # Match when the row's principal_id equals our id for that type OR is the
            # tenant wildcard '*'.
            mine = pid_for_type.get(ptype)
            matches = pid == "*" or (mine is not None and pid == mine)
            if matches and operation in (row["permissions"] or []):
                return True
        return False

    allowed = await in_tenant(pool, principal.tenant_id, _txn)
    if not allowed:
        metrics.acl_denied_total.labels(operation).inc()
        logger.info("kb_acl_denied", kb_id=kb_id, operation=operation, tenant=principal.tenant_id)
        raise ApiError(
            ErrorCode.FORBIDDEN_KB,
            f"Principal is not permitted to {operation} this knowledge base.",
        )
