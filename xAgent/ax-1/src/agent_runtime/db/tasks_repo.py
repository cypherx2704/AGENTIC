"""Repository functions for ``xagent.tasks`` (RLS-scoped, tenant-isolated).

``create_task`` inserts the ``pending`` row at submission time. The EVENT stage calls
``finalize_task`` (the terminal UPDATE) *together with* the outbox INSERT in ONE tenant
transaction — that atomic write lives in ``db/outbox.py`` (``record_task_event``), not
here, so the task row and the Kafka event can never diverge (Component 3b). The helpers
here cover the non-atomic state transitions (create, mark-running, get).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .pool import in_tenant

# Terminal statuses (xagent.tasks.status CHECK enum).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})


@dataclass
class TaskRow:
    """In-process view of a ``xagent.tasks`` row (text-cast UUIDs, ISO timestamps)."""

    task_id: str
    agent_id: str
    tenant_id: str
    trace_id: str
    status: str
    input: dict[str, Any]
    user_id: str | None = None
    # Optional conversational-session correlator (WP12) — scopes session memory; NOT identity.
    session_id: str | None = None
    # Optional per-task USD cost budget (WP12) — the LLM/tool stages accrue against it.
    cost_budget_per_task: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_msg: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    timeout_at: str | None = None
    # Subtask lineage (migration 0008) — set only for orchestration sub-agent tasks;
    # both NULL for standalone single-agent tasks (the public POST /v1/tasks path).
    parent_task_id: str | None = None
    workflow_id: str | None = None


_SELECT_COLUMNS = """
    task_id::text AS task_id, agent_id::text AS agent_id, tenant_id::text AS tenant_id,
    user_id::text AS user_id, trace_id::text AS trace_id, status, input, metadata, output,
    session_id, cost_budget_per_task,
    parent_task_id::text AS parent_task_id, workflow_id::text AS workflow_id,
    error_code, error_msg, tokens_used, cost_usd,
    to_char(created_at,   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS created_at,
    to_char(started_at,   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS started_at,
    to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS completed_at,
    to_char(timeout_at,   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS timeout_at
"""


def _row_to_task(row: dict[str, Any]) -> TaskRow:
    cost = row.get("cost_usd")
    return TaskRow(
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        status=row["status"],
        input=row["input"],
        user_id=row.get("user_id"),
        session_id=row.get("session_id"),
        cost_budget_per_task=(
            float(row["cost_budget_per_task"]) if row.get("cost_budget_per_task") is not None else None
        ),
        metadata=row.get("metadata") or {},
        output=row.get("output"),
        error_code=row.get("error_code"),
        error_msg=row.get("error_msg"),
        tokens_used=row.get("tokens_used"),
        cost_usd=float(cost) if cost is not None else None,
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        timeout_at=row.get("timeout_at"),
        parent_task_id=row.get("parent_task_id"),
        workflow_id=row.get("workflow_id"),
    )


async def create_task(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_id: str,
    trace_id: str,
    task_input: dict[str, Any],
    timeout_seconds: int,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    session_id: str | None = None,
    cost_budget_per_task: float | None = None,
    parent_task_id: str | None = None,
    workflow_id: str | None = None,
) -> TaskRow:
    """INSERT a new ``pending`` task row and return it (task_id is DB-generated).

    ``session_id`` / ``cost_budget_per_task`` (WP12) are OPTIONAL — both default NULL
    (no session correlation / no cost cap) so the first-cycle call path is unchanged.
    ``parent_task_id`` / ``workflow_id`` (0008) are set ONLY by the orchestration engine
    when it spawns a sub-agent task; both default NULL so the public POST /v1/tasks path
    is byte-identical to before.
    """

    async def _q(conn: AsyncConnection) -> TaskRow:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            INSERT INTO xagent.tasks
              (agent_id, tenant_id, user_id, trace_id, status, input, metadata,
               session_id, cost_budget_per_task, parent_task_id, workflow_id, timeout_at)
            VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s,
                    NOW() + (%s || ' seconds')::interval)
            RETURNING {_SELECT_COLUMNS}
            """,  # noqa: S608 — static RETURNING columns
            (
                agent_id,
                tenant_id,
                user_id,
                trace_id,
                Jsonb(task_input),
                Jsonb(metadata or {}),
                session_id,
                cost_budget_per_task,
                parent_task_id,
                workflow_id,
                str(timeout_seconds),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return _row_to_task(row)

    return await in_tenant(pool, tenant_id, _q)


async def mark_running(pool: AsyncConnectionPool, tenant_id: str, task_id: str) -> None:
    """Transition a task to ``running`` and stamp ``started_at`` (pipeline start)."""

    async def _q(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE xagent.tasks SET status = 'running', started_at = NOW() WHERE task_id = %s",
            (task_id,),
        )

    await in_tenant(pool, tenant_id, _q)


async def get_task(pool: AsyncConnectionPool, tenant_id: str, task_id: str) -> TaskRow | None:
    """Return the (RLS-scoped) task row for ``GET /v1/tasks/{task_id}``, or None."""

    async def _q(conn: AsyncConnection) -> TaskRow | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_SELECT_COLUMNS} FROM xagent.tasks WHERE task_id = %s",  # noqa: S608 — static columns
            (task_id,),
        )
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

    return await in_tenant(pool, tenant_id, _q)


# ── Cursor-paginated task list (GET /v1/tasks — Task Feed dependency, WP08) ───────────
# Redaction-safe projection: the list NEVER returns ``input`` / ``output`` / ``error_msg``
# (free-form, possibly-sensitive payloads). It returns the task envelope the Task Feed
# needs — ids, status, usage, timestamps, error_code, metadata — RLS-scoped to the tenant.
@dataclass
class TaskListItem:
    """One row of the GET /v1/tasks list — a redaction-safe task summary."""

    task_id: str
    agent_id: str
    status: str
    trace_id: str
    error_code: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


_LIST_COLUMNS = """
    task_id::text AS task_id, agent_id::text AS agent_id, status, trace_id::text AS trace_id,
    error_code, tokens_used, cost_usd, metadata,
    to_char(created_at,   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS created_at,
    to_char(started_at,   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS started_at,
    to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS completed_at
"""


async def list_tasks(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    limit: int,
    cursor_created_at: str | None = None,
    cursor_task_id: str | None = None,
    since: str | None = None,
    status: str | None = None,
    agent_id: str | None = None,
) -> list[TaskListItem]:
    """List tasks newest-first with keyset cursor pagination (RLS-scoped to ``tenant_id``).

    Ordering is ``(created_at DESC, task_id DESC)`` — a stable total order so the keyset
    cursor never skips or repeats a row even when many tasks share a ``created_at``. The
    cursor is the ``(created_at, task_id)`` of the last row of the previous page; pass both
    to fetch the strictly-older page. Filters (all optional, AND-combined): ``since`` (only
    tasks created at/after an RFC 3339 instant), ``status``, ``agent_id``. ``limit`` rows
    are returned (the caller fetches ``page_size + 1`` to detect a next page).
    """

    async def _q(conn: AsyncConnection) -> list[TaskListItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("created_at >= %s")
            params.append(since)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if agent_id is not None:
            clauses.append("agent_id = %s")
            params.append(agent_id)
        if cursor_created_at is not None and cursor_task_id is not None:
            # Keyset: rows strictly "older" than the cursor in the (created_at, task_id) order.
            clauses.append("(created_at, task_id) < (%s, %s)")
            params.extend([cursor_created_at, cursor_task_id])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT {_LIST_COLUMNS}
              FROM xagent.tasks
              {where}
             ORDER BY created_at DESC, task_id DESC
             LIMIT %s
            """,  # noqa: S608 — static columns; WHERE built from a fixed clause whitelist, values are bound params
            tuple(params),
        )
        rows = await cur.fetchall()
        return [_row_to_list_item(r) for r in rows]

    return await in_tenant(pool, tenant_id, _q)


def _row_to_list_item(row: dict[str, Any]) -> TaskListItem:
    cost = row.get("cost_usd")
    return TaskListItem(
        task_id=row["task_id"],
        agent_id=row["agent_id"],
        status=row["status"],
        trace_id=row["trace_id"],
        error_code=row.get("error_code"),
        tokens_used=row.get("tokens_used"),
        cost_usd=float(cost) if cost is not None else None,
        metadata=row.get("metadata") or {},
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
    )


# ── Backup-sweeper discovery (cross-tenant; runs under the sweeper RLS bypass) ────────
@dataclass
class StuckTask:
    """A non-terminal task found by the sweeper past its deadline (minimal identity)."""

    task_id: str
    tenant_id: str
    agent_id: str
    trace_id: str
    status: str


async def list_stuck_tasks(conn: AsyncConnection, *, grace_seconds: int, limit: int) -> list[StuckTask]:
    """Find non-terminal tasks whose ``timeout_at`` is older than NOW() - grace.

    MUST run inside a transaction where ``app.sweeper = 'on'`` is set (the sweeper
    bypass policy) so the cross-tenant discovery is visible despite RLS. Ordered oldest
    -first and bounded by ``limit`` so each wake does a fixed amount of work.
    """
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT task_id::text AS task_id, tenant_id::text AS tenant_id,
               agent_id::text AS agent_id, trace_id::text AS trace_id, status
          FROM xagent.tasks
         WHERE status IN ('pending', 'running')
           AND timeout_at IS NOT NULL
           AND timeout_at < NOW() - (%s || ' seconds')::interval
         ORDER BY timeout_at
         LIMIT %s
        """,
        (str(grace_seconds), limit),
    )
    rows = await cur.fetchall()
    return [
        StuckTask(
            task_id=r["task_id"],
            tenant_id=r["tenant_id"],
            agent_id=r["agent_id"],
            trace_id=r["trace_id"],
            status=r["status"],
        )
        for r in rows
    ]


async def delete_old_outbox(conn: AsyncConnection, *, retention_days: int) -> int:
    """Delete PUBLISHED outbox rows older than ``retention_days``; return the row count.

    outbox has no RLS (it is the cross-tenant publish queue), so this needs no sweeper
    GUC — but it is harmless to run inside the sweeper transaction alongside task_steps.
    """
    cur = await conn.execute(
        """
        DELETE FROM xagent.outbox
         WHERE published_at IS NOT NULL
           AND published_at < NOW() - (%s || ' days')::interval
        """,
        (str(retention_days),),
    )
    return cur.rowcount


async def delete_old_task_steps(conn: AsyncConnection, *, retention_days: int) -> int:
    """Delete ``task_steps`` older than ``retention_days``; return the row count.

    MUST run with ``app.sweeper = 'on'`` set (task_steps is RLS'd) so the delete is not
    silently scoped to a single (unset) tenant.
    """
    cur = await conn.execute(
        """
        DELETE FROM xagent.task_steps
         WHERE created_at < NOW() - (%s || ' days')::interval
        """,
        (str(retention_days),),
    )
    return cur.rowcount


def now_iso() -> str:
    """RFC 3339 UTC ms-precision timestamp (response started_at/completed_at helper)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
