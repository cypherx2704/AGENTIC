"""RLS-scoped persistence for the orchestration engine (migration 0008).

Mirrors the :mod:`agent_runtime.db.tasks_repo` conventions: every read/write runs inside
``in_tenant(pool, tenant_id, ...)`` (sets ``app.tenant_id`` transaction-local) so RLS admits
only the caller's tenant; timestamps are ``to_char``-formatted to RFC 3339 UTC ms; NUMERIC
columns are cast to ``float`` in the row mappers.

State-transition writes use OPTIMISTIC LOCKING: ``UPDATE ... WHERE ... AND version = %s``
bumping ``version = version + 1``; a version mismatch returns ``None`` so the caller re-reads
and retries the state-machine step (fan-in / synthesis nodes race on the same row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..db.pool import in_tenant
from .authz import AgentHierarchy

_TS = "'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'"


# ── agent hierarchy (for the authz guards) ─────────────────────────────────────────────
async def get_agent_hierarchy(
    pool: AsyncConnectionPool, tenant_id: str, agent_id: str
) -> AgentHierarchy | None:
    """Read an agent's hierarchy facts from the ``xagent.agents`` mirror (RLS-scoped).

    Returns ``None`` when the agent is not visible in ``tenant_id`` (RLS-hidden / unknown) —
    the authz guard maps that to ``NOT_FOUND``.
    """

    async def _q(conn: AsyncConnection) -> AgentHierarchy | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT agent_id::text AS agent_id, agent_type,
                   parent_orchestrator_id::text AS parent_orchestrator_id
              FROM xagent.agents
             WHERE agent_id = %s
            """,
            (agent_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return AgentHierarchy(
            agent_id=row["agent_id"],
            agent_type=row["agent_type"],
            parent_orchestrator_id=row.get("parent_orchestrator_id"),
        )

    return await in_tenant(pool, tenant_id, _q)


# ── roster (the orchestrator's runnable sub-agents) ────────────────────────────────────
@dataclass
class SubAgentRef:
    """A runnable sub-agent of the orchestrator (from the xagent.agents runtime mirror).

    Carries CAPABILITY, not just identity: the planner routes a step to an agent, so it must know
    what each one is FOR (:attr:`description`) and what it can actually DO (:attr:`allowed_tools`).
    With names alone it can only guess from the string — and will happily hand a GitHub lookup to
    an agent that only holds a Wikipedia tool, which then answers from thin air.
    """

    agent_id: str
    name: str
    #: Routing description (migration 0009) — "when to use this agent", written for the planner.
    description: str = ""
    #: The agent's own instructions. Only a FALLBACK purpose, for agents created before 0009 had a
    #: description; it addresses the agent, not the router, so it is a poor routing signal.
    system_prompt: str = ""
    #: Tool/MCP-server names the agent may call. Empty = no tools (pure LLM).
    allowed_tools: tuple[str, ...] = ()

    @property
    def purpose(self) -> str:
        """What the planner is shown as this agent's reason to exist (description, else prompt)."""
        return self.description.strip() or self.system_prompt.strip()


async def list_orchestrator_subagents(
    pool: AsyncConnectionPool, tenant_id: str, orchestrator_id: str
) -> list[SubAgentRef]:
    """List the ACTIVE sub-agents owned by ``orchestrator_id`` that have a runtime row (roster source).

    Reads ``xagent.agents`` (the runtime mirror) — only agents with a registered runtime can actually
    run — filtered to ``agent_type='sub_agent'`` + ``parent_orchestrator_id=orchestrator`` + active.
    The driver maps a node's ``preset`` to a sub-agent by NAME (presets materialize as sub-agents).
    """

    async def _q(conn: AsyncConnection) -> list[SubAgentRef]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT agent_id::text AS agent_id, name, description, system_prompt, allowed_tools
              FROM xagent.agents
             WHERE parent_orchestrator_id = %s
               AND agent_type = 'sub_agent'
               AND status = 'active'
             ORDER BY name
            """,
            (orchestrator_id,),
        )
        return [
            SubAgentRef(
                agent_id=r["agent_id"],
                name=r["name"],
                description=r["description"] or "",
                system_prompt=r["system_prompt"] or "",
                allowed_tools=tuple(r["allowed_tools"] or ()),
            )
            for r in await cur.fetchall()
        ]

    return await in_tenant(pool, tenant_id, _q)


# ── workflows ──────────────────────────────────────────────────────────────────────────
@dataclass
class WorkflowRow:
    """In-process view of a ``xagent.workflows`` row."""

    workflow_id: str
    tenant_id: str
    root_agent_id: str
    goal: str
    status: str
    mode: str
    version: int
    decomposition: str | None = None
    subtask_dag: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_msg: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    cost_budget_usd: float | None = None
    approval_due_at: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    timeout_at: str | None = None


_WORKFLOW_COLUMNS = f"""
    workflow_id::text AS workflow_id, tenant_id::text AS tenant_id,
    root_agent_id::text AS root_agent_id, goal, status, mode, decomposition,
    subtask_dag, output, error_code, error_msg, tokens_used, cost_usd, cost_budget_usd,
    to_char(approval_due_at, {_TS}) AS approval_due_at,
    to_char(created_at,      {_TS}) AS created_at,
    to_char(started_at,      {_TS}) AS started_at,
    to_char(completed_at,    {_TS}) AS completed_at,
    to_char(timeout_at,      {_TS}) AS timeout_at,
    version
"""


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def _row_to_workflow(row: dict[str, Any]) -> WorkflowRow:
    return WorkflowRow(
        workflow_id=row["workflow_id"],
        tenant_id=row["tenant_id"],
        root_agent_id=row["root_agent_id"],
        goal=row["goal"],
        status=row["status"],
        mode=row["mode"],
        version=row["version"],
        decomposition=row.get("decomposition"),
        subtask_dag=row.get("subtask_dag"),
        output=row.get("output"),
        error_code=row.get("error_code"),
        error_msg=row.get("error_msg"),
        tokens_used=row.get("tokens_used"),
        cost_usd=_num(row.get("cost_usd")),
        cost_budget_usd=_num(row.get("cost_budget_usd")),
        approval_due_at=row.get("approval_due_at"),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        timeout_at=row.get("timeout_at"),
    )


async def create_workflow(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    root_agent_id: str,
    goal: str,
    mode: str = "subagents",
    cost_budget_usd: float | None = None,
    timeout_seconds: int | None = None,
) -> WorkflowRow:
    """INSERT a new ``pending`` workflow run and return it (workflow_id DB-generated)."""

    async def _q(conn: AsyncConnection) -> WorkflowRow:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            INSERT INTO xagent.workflows
              (tenant_id, root_agent_id, goal, status, mode, cost_budget_usd, timeout_at)
            VALUES (%s, %s, %s, 'pending', %s, %s,
                    CASE WHEN %s::int IS NULL THEN NULL
                         ELSE NOW() + (%s || ' seconds')::interval END)
            RETURNING {_WORKFLOW_COLUMNS}
            """,  # noqa: S608 — static RETURNING columns
            (tenant_id, root_agent_id, goal, mode, cost_budget_usd, timeout_seconds, timeout_seconds),
        )
        row = await cur.fetchone()
        assert row is not None
        return _row_to_workflow(row)

    return await in_tenant(pool, tenant_id, _q)


async def get_workflow(
    pool: AsyncConnectionPool, tenant_id: str, workflow_id: str
) -> WorkflowRow | None:
    """Return the RLS-scoped workflow row, or ``None``."""

    async def _q(conn: AsyncConnection) -> WorkflowRow | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_WORKFLOW_COLUMNS} FROM xagent.workflows WHERE workflow_id = %s",  # noqa: S608
            (workflow_id,),
        )
        row = await cur.fetchone()
        return _row_to_workflow(row) if row else None

    return await in_tenant(pool, tenant_id, _q)


#: Columns the optimistic workflow updater may set (whitelist — values are always bound params).
_WORKFLOW_UPDATABLE = frozenset(
    {"status", "decomposition", "error_code", "error_msg", "tokens_used", "cost_usd", "approval_due_at"}
)


async def update_workflow(
    pool: AsyncConnectionPool,
    tenant_id: str,
    workflow_id: str,
    *,
    expected_version: int,
    subtask_dag: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    mark_started: bool = False,
    mark_completed: bool = False,
    **fields: Any,
) -> WorkflowRow | None:
    """Optimistically update a workflow row; bump ``version``; return the row or ``None``.

    ``fields`` may contain any key in :data:`_WORKFLOW_UPDATABLE` (others raise ``ValueError`` —
    a programming error, never caller input). ``subtask_dag`` / ``output`` are JSONB-wrapped;
    ``mark_started`` / ``mark_completed`` stamp ``started_at`` / ``completed_at = NOW()``.
    Returns ``None`` on a version mismatch (the row moved under us — caller re-reads + retries).
    """
    bad = set(fields) - _WORKFLOW_UPDATABLE
    if bad:
        raise ValueError(f"Non-updatable workflow field(s): {sorted(bad)}")

    set_parts: list[str] = ["version = version + 1"]
    params: list[Any] = []
    for col, value in fields.items():
        set_parts.append(f"{col} = %s")
        params.append(value)
    if subtask_dag is not None:
        set_parts.append("subtask_dag = %s")
        params.append(Jsonb(subtask_dag))
    if output is not None:
        set_parts.append("output = %s")
        params.append(Jsonb(output))
    if mark_started:
        set_parts.append("started_at = NOW()")
    if mark_completed:
        set_parts.append("completed_at = NOW()")

    params.extend([workflow_id, expected_version])

    async def _q(conn: AsyncConnection) -> WorkflowRow | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            UPDATE xagent.workflows SET {", ".join(set_parts)}
             WHERE workflow_id = %s AND version = %s
            RETURNING {_WORKFLOW_COLUMNS}
            """,  # noqa: S608 — SET cols are a fixed whitelist; all values are bound params
            tuple(params),
        )
        row = await cur.fetchone()
        return _row_to_workflow(row) if row else None

    return await in_tenant(pool, tenant_id, _q)


async def list_workflows(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    limit: int,
    root_agent_id: str | None = None,
    status: str | None = None,
) -> list[WorkflowRow]:
    """List workflows newest-first (RLS-scoped), optionally filtered by orchestrator/status."""

    async def _q(conn: AsyncConnection) -> list[WorkflowRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if root_agent_id is not None:
            clauses.append("root_agent_id = %s")
            params.append(root_agent_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT {_WORKFLOW_COLUMNS} FROM xagent.workflows
            {where}
             ORDER BY created_at DESC, workflow_id DESC
             LIMIT %s
            """,  # noqa: S608 — static columns; WHERE from a fixed clause whitelist, values bound
            tuple(params),
        )
        return [_row_to_workflow(r) for r in await cur.fetchall()]

    return await in_tenant(pool, tenant_id, _q)


# ── workflow_tasks (DAG nodes) ─────────────────────────────────────────────────────────
@dataclass
class WorkflowTaskRow:
    """In-process view of a ``xagent.workflow_tasks`` row (one DAG node)."""

    id: str
    workflow_id: str
    tenant_id: str
    node_id: str
    node_type: str
    status: str
    version: int
    task_id: str | None = None
    parent_node_id: str | None = None
    description: str = ""
    assigned_agent_id: str | None = None
    preset: str | None = None
    depends_on: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    retry_count: int = 0
    retry_max: int = 1
    approval_request_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


_WORKFLOW_TASK_COLUMNS = f"""
    id::text AS id, workflow_id::text AS workflow_id, tenant_id::text AS tenant_id,
    node_id, task_id::text AS task_id, parent_node_id, description, node_type,
    assigned_agent_id::text AS assigned_agent_id, preset, depends_on, status, output,
    tokens_used, cost_usd, retry_count, retry_max,
    approval_request_id::text AS approval_request_id,
    to_char(created_at,   {_TS}) AS created_at,
    to_char(started_at,   {_TS}) AS started_at,
    to_char(completed_at, {_TS}) AS completed_at,
    version
"""


def _row_to_workflow_task(row: dict[str, Any]) -> WorkflowTaskRow:
    return WorkflowTaskRow(
        id=row["id"],
        workflow_id=row["workflow_id"],
        tenant_id=row["tenant_id"],
        node_id=row["node_id"],
        node_type=row["node_type"],
        status=row["status"],
        version=row["version"],
        task_id=row.get("task_id"),
        parent_node_id=row.get("parent_node_id"),
        description=row.get("description") or "",
        assigned_agent_id=row.get("assigned_agent_id"),
        preset=row.get("preset"),
        depends_on=list(row.get("depends_on") or []),
        output=row.get("output"),
        tokens_used=row.get("tokens_used"),
        cost_usd=_num(row.get("cost_usd")),
        retry_count=int(row.get("retry_count") or 0),
        retry_max=int(row["retry_max"]) if row.get("retry_max") is not None else 1,
        approval_request_id=row.get("approval_request_id"),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
    )


async def create_workflow_task(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    workflow_id: str,
    node_id: str,
    node_type: str = "agent",
    description: str = "",
    assigned_agent_id: str | None = None,
    preset: str | None = None,
    depends_on: list[str] | None = None,
    parent_node_id: str | None = None,
    retry_max: int = 1,
) -> WorkflowTaskRow:
    """INSERT a ``pending`` DAG node for a workflow and return it."""

    async def _q(conn: AsyncConnection) -> WorkflowTaskRow:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            INSERT INTO xagent.workflow_tasks
              (workflow_id, tenant_id, node_id, node_type, description, assigned_agent_id,
               preset, depends_on, parent_node_id, retry_max, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING {_WORKFLOW_TASK_COLUMNS}
            """,  # noqa: S608 — static RETURNING columns
            (
                workflow_id,
                tenant_id,
                node_id,
                node_type,
                description,
                assigned_agent_id,
                preset,
                depends_on or [],
                parent_node_id,
                retry_max,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return _row_to_workflow_task(row)

    return await in_tenant(pool, tenant_id, _q)


async def list_workflow_tasks(
    pool: AsyncConnectionPool, tenant_id: str, workflow_id: str
) -> list[WorkflowTaskRow]:
    """Return all DAG nodes for a workflow (RLS-scoped), creation order."""

    async def _q(conn: AsyncConnection) -> list[WorkflowTaskRow]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT {_WORKFLOW_TASK_COLUMNS} FROM xagent.workflow_tasks
             WHERE workflow_id = %s
             ORDER BY created_at, node_id
            """,  # noqa: S608 — static columns
            (workflow_id,),
        )
        return [_row_to_workflow_task(r) for r in await cur.fetchall()]

    return await in_tenant(pool, tenant_id, _q)


#: Columns the optimistic node updater may set (whitelist).
_WORKFLOW_TASK_UPDATABLE = frozenset(
    {
        "status",
        "task_id",
        "assigned_agent_id",
        "tokens_used",
        "cost_usd",
        "retry_count",
        "approval_request_id",
    }
)


async def update_workflow_task(
    pool: AsyncConnectionPool,
    tenant_id: str,
    node_pk: str,
    *,
    expected_version: int,
    output: dict[str, Any] | None = None,
    mark_started: bool = False,
    mark_completed: bool = False,
    **fields: Any,
) -> WorkflowTaskRow | None:
    """Optimistically update a DAG node by primary key ``id``; bump ``version``.

    Returns ``None`` on a version mismatch (a concurrent completer won the race — the caller
    re-reads the row and re-runs the state-machine step). ``fields`` keys are restricted to
    :data:`_WORKFLOW_TASK_UPDATABLE`.
    """
    bad = set(fields) - _WORKFLOW_TASK_UPDATABLE
    if bad:
        raise ValueError(f"Non-updatable workflow_task field(s): {sorted(bad)}")

    set_parts: list[str] = ["version = version + 1"]
    params: list[Any] = []
    for col, value in fields.items():
        set_parts.append(f"{col} = %s")
        params.append(value)
    if output is not None:
        set_parts.append("output = %s")
        params.append(Jsonb(output))
    if mark_started:
        set_parts.append("started_at = NOW()")
    if mark_completed:
        set_parts.append("completed_at = NOW()")

    params.extend([node_pk, expected_version])

    async def _q(conn: AsyncConnection) -> WorkflowTaskRow | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            UPDATE xagent.workflow_tasks SET {", ".join(set_parts)}
             WHERE id = %s AND version = %s
            RETURNING {_WORKFLOW_TASK_COLUMNS}
            """,  # noqa: S608 — SET cols are a fixed whitelist; values are bound params
            tuple(params),
        )
        row = await cur.fetchone()
        return _row_to_workflow_task(row) if row else None

    return await in_tenant(pool, tenant_id, _q)


# ── agent_presets ──────────────────────────────────────────────────────────────────────
@dataclass
class AgentPresetRow:
    """In-process view of a ``xagent.agent_presets`` row."""

    preset_id: str
    tenant_id: str
    name: str
    description: str | None = None
    system_prompt: str | None = None
    model_alias: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    allowed_scopes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


_PRESET_COLUMNS = f"""
    preset_id::text AS preset_id, tenant_id::text AS tenant_id, name, description,
    system_prompt, model_alias, allowed_tools, allowed_scopes, metadata,
    to_char(created_at, {_TS}) AS created_at,
    to_char(updated_at, {_TS}) AS updated_at
"""


def _row_to_preset(row: dict[str, Any]) -> AgentPresetRow:
    return AgentPresetRow(
        preset_id=row["preset_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        description=row.get("description"),
        system_prompt=row.get("system_prompt"),
        model_alias=row.get("model_alias"),
        allowed_tools=list(row.get("allowed_tools") or []),
        allowed_scopes=list(row.get("allowed_scopes") or []),
        metadata=row.get("metadata") or {},
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def create_preset(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    name: str,
    description: str | None = None,
    system_prompt: str | None = None,
    model_alias: str | None = None,
    allowed_tools: list[str] | None = None,
    allowed_scopes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentPresetRow:
    """INSERT a sub-agent preset (unique per ``(tenant_id, name)``) and return it."""

    async def _q(conn: AsyncConnection) -> AgentPresetRow:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            INSERT INTO xagent.agent_presets
              (tenant_id, name, description, system_prompt, model_alias,
               allowed_tools, allowed_scopes, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_PRESET_COLUMNS}
            """,  # noqa: S608 — static RETURNING columns
            (
                tenant_id,
                name,
                description,
                system_prompt,
                model_alias,
                allowed_tools or [],
                allowed_scopes or [],
                Jsonb(metadata or {}),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return _row_to_preset(row)

    return await in_tenant(pool, tenant_id, _q)


async def list_presets(pool: AsyncConnectionPool, tenant_id: str) -> list[AgentPresetRow]:
    """List the tenant's sub-agent presets (RLS-scoped), by name."""

    async def _q(conn: AsyncConnection) -> list[AgentPresetRow]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_PRESET_COLUMNS} FROM xagent.agent_presets ORDER BY name",  # noqa: S608
        )
        return [_row_to_preset(r) for r in await cur.fetchall()]

    return await in_tenant(pool, tenant_id, _q)
