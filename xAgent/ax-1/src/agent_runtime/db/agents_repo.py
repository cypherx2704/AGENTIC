"""Repository functions for ``xagent.agents`` (RLS-scoped, tenant-isolated).

All reads/writes run inside ``in_tenant(pool, tenant_id, ...)`` so RLS admits only the
caller's tenant. ``agent_id`` is the same UUID as ``auth.agents.agent_id`` (no
cross-schema FK).

Lifecycle (WP08):
  * ``get_agent``              — read the runtime row (LOAD stage + GET endpoint).
  * ``upsert_agent_runtime``   — create-only (ON CONFLICT DO NOTHING + re-select);
    idempotent on agent_id. Retained for the create-only POST back-compat.
  * ``insert_agent_runtime``   — INSERT the row, returning it; assumes it does not exist
    (the PUT endpoint calls this only after a get_agent miss).
  * ``update_agent_runtime``   — UPDATE the existing row with the new config + status +
    a caller-supplied (already-bumped) runtime_version, stamping ``updated_at = NOW()``.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..models.agent import AgentRuntime, AgentRuntimeRegistration
from .pool import in_tenant

_COLUMNS = """
    agent_id::text AS agent_id, tenant_id::text AS tenant_id, name, runtime_version, status,
    llm_model, system_prompt, max_tokens, temperature, memory_scope,
    guardrail_policy_id::text AS guardrail_policy_id, allowed_tools, allowed_skills,
    allowed_kb_ids::text[] AS allowed_kb_ids, rag_top_k_per_kb, rag_min_score,
    token_budget_per_task, capabilities, metadata
"""


def _row_to_runtime(row: dict[str, Any]) -> AgentRuntime:
    # psycopg returns UUID[] -> list[str] already cast via ::text[]; JSONB -> python.
    return AgentRuntime.model_validate(row)


async def get_agent(pool: AsyncConnectionPool, tenant_id: str, agent_id: str) -> AgentRuntime | None:
    """Return the runtime config for ``agent_id`` within ``tenant_id``, or None.

    Used by the LOAD stage and the capabilities endpoint. RLS guarantees a row from a
    different tenant is invisible (returns None).
    """

    async def _q(conn: AsyncConnection) -> AgentRuntime | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLUMNS} FROM xagent.agents WHERE agent_id = %s",  # noqa: S608 — static columns
            (agent_id,),
        )
        row = await cur.fetchone()
        return _row_to_runtime(row) if row else None

    return await in_tenant(pool, tenant_id, _q)


async def upsert_agent_runtime(
    pool: AsyncConnectionPool,
    tenant_id: str,
    agent_id: str,
    reg: AgentRuntimeRegistration,
) -> AgentRuntime:
    """Insert (or return existing) ``xagent.agents`` row. Idempotent on agent_id.

    ON CONFLICT (agent_id) DO NOTHING then re-select, so a duplicate registration
    returns the EXISTING row unchanged (Component 1 idempotency rule).
    """

    async def _q(conn: AsyncConnection) -> AgentRuntime:
        await conn.execute(
            """
            INSERT INTO xagent.agents
              (agent_id, tenant_id, name, runtime_version, status, llm_model, system_prompt,
               max_tokens, temperature, memory_scope, guardrail_policy_id, allowed_tools,
               allowed_skills, allowed_kb_ids, rag_top_k_per_kb, rag_min_score,
               token_budget_per_task, capabilities, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (agent_id) DO NOTHING
            """,
            (
                agent_id,
                tenant_id,
                reg.name,
                reg.runtime_version,
                reg.status,
                reg.llm_model,
                reg.system_prompt,
                reg.max_tokens,
                reg.temperature,
                reg.memory_scope,
                reg.guardrail_policy_id,
                reg.allowed_tools,
                reg.allowed_skills,
                reg.allowed_kb_ids,
                reg.rag_top_k_per_kb,
                reg.rag_min_score,
                reg.token_budget_per_task,
                Jsonb(reg.capabilities),
                Jsonb(reg.metadata),
            ),
        )
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLUMNS} FROM xagent.agents WHERE agent_id = %s",  # noqa: S608 — static columns
            (agent_id,),
        )
        row = await cur.fetchone()
        assert row is not None  # the INSERT-or-existing row must exist now
        return _row_to_runtime(row)

    return await in_tenant(pool, tenant_id, _q)


async def insert_agent_runtime(
    pool: AsyncConnectionPool,
    tenant_id: str,
    agent_id: str,
    reg: AgentRuntimeRegistration,
) -> AgentRuntime:
    """INSERT a new ``xagent.agents`` row and return it (PUT create-path; row must be new).

    Unlike :func:`upsert_agent_runtime` (ON CONFLICT DO NOTHING), this performs a plain
    INSERT — the PUT endpoint calls it only after a :func:`get_agent` miss under the same
    request, so the row is expected not to exist. ``ON CONFLICT (agent_id) DO NOTHING``
    is kept as a concurrency guard, then the row is re-selected (the concurrent writer's
    row wins — still correct + idempotent).
    """

    async def _q(conn: AsyncConnection) -> AgentRuntime:
        await conn.execute(
            """
            INSERT INTO xagent.agents
              (agent_id, tenant_id, name, runtime_version, status, llm_model, system_prompt,
               max_tokens, temperature, memory_scope, guardrail_policy_id, allowed_tools,
               allowed_skills, allowed_kb_ids, rag_top_k_per_kb, rag_min_score,
               token_budget_per_task, capabilities, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (agent_id) DO NOTHING
            """,
            (
                agent_id,
                tenant_id,
                reg.name,
                reg.runtime_version,
                reg.status,
                reg.llm_model,
                reg.system_prompt,
                reg.max_tokens,
                reg.temperature,
                reg.memory_scope,
                reg.guardrail_policy_id,
                reg.allowed_tools,
                reg.allowed_skills,
                reg.allowed_kb_ids,
                reg.rag_top_k_per_kb,
                reg.rag_min_score,
                reg.token_budget_per_task,
                Jsonb(reg.capabilities),
                Jsonb(reg.metadata),
            ),
        )
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLUMNS} FROM xagent.agents WHERE agent_id = %s",  # noqa: S608 — static columns
            (agent_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return _row_to_runtime(row)

    return await in_tenant(pool, tenant_id, _q)


async def update_agent_runtime(
    pool: AsyncConnectionPool,
    tenant_id: str,
    agent_id: str,
    reg: AgentRuntimeRegistration,
    *,
    runtime_version: str,
    status: str,
) -> AgentRuntime | None:
    """UPDATE an existing runtime row with the new config + status + ``runtime_version``.

    ``runtime_version`` is the (already-bumped) value the API layer computed; ``status``
    is the (already-transition-validated) target. Stamps ``updated_at = NOW()``. Returns
    the updated row, or ``None`` when no row matched (RLS-hidden / cross-tenant / deleted
    between the read and the write) so the caller can surface a 404.
    """

    async def _q(conn: AsyncConnection) -> AgentRuntime | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            UPDATE xagent.agents
               SET name = %s, runtime_version = %s, status = %s, llm_model = %s,
                   system_prompt = %s, max_tokens = %s, temperature = %s, memory_scope = %s,
                   guardrail_policy_id = %s, allowed_tools = %s, allowed_skills = %s,
                   allowed_kb_ids = %s, rag_top_k_per_kb = %s, rag_min_score = %s,
                   token_budget_per_task = %s, capabilities = %s, metadata = %s,
                   updated_at = NOW()
             WHERE agent_id = %s
            RETURNING {_COLUMNS}
            """,  # noqa: S608 — static RETURNING columns
            (
                reg.name,
                runtime_version,
                status,
                reg.llm_model,
                reg.system_prompt,
                reg.max_tokens,
                reg.temperature,
                reg.memory_scope,
                reg.guardrail_policy_id,
                reg.allowed_tools,
                reg.allowed_skills,
                reg.allowed_kb_ids,
                reg.rag_top_k_per_kb,
                reg.rag_min_score,
                reg.token_budget_per_task,
                Jsonb(reg.capabilities),
                Jsonb(reg.metadata),
                agent_id,
            ),
        )
        row = await cur.fetchone()
        return _row_to_runtime(row) if row else None

    return await in_tenant(pool, tenant_id, _q)
