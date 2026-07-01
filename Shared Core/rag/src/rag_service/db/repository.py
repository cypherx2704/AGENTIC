"""Tenant-scoped data access for knowledge bases, documents, and ACLs.

Every function runs inside an ``in_tenant`` transaction so RLS gates the tenant. Returns
plain dicts (dict_row) so the API layer maps straight to pydantic responses. The KB
creation path also writes the default ``(tenant,'*')`` ACL row (unless ``private``) in the
SAME transaction (Component 5c requirement).
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..core.errors import ApiError, ErrorCode
from .pool import in_tenant

_DEFAULT_ACL_PERMS = ["read", "query", "ingest", "write", "admin"]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z") if hasattr(value, "isoformat") else str(value)


def _kb_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kb_id": str(row["kb_id"]),
        "tenant_id": str(row["tenant_id"]),
        "name": row["name"],
        "description": row["description"],
        "chunking_strategy": row["chunking_strategy"],
        "chunk_size": row["chunk_size"],
        "chunk_overlap": row["chunk_overlap"],
        "embedding_model_alias": row["embedding_model_alias"],
        "embedding_model_resolved": row["embedding_model_resolved"],
        "embedding_dim": row["embedding_dim"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


# ── Knowledge bases ─────────────────────────────────────────────────────────────
async def create_kb(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    name: str,
    description: str | None,
    chunking_strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    embedding_model_alias: str,
    embedding_model_resolved: str,
    embedding_dim: int,
    created_by: str,
    private: bool,
) -> dict[str, Any]:
    async def _txn(conn: AsyncConnection) -> dict[str, Any]:
        cur = conn.cursor(row_factory=dict_row)
        try:
            await cur.execute(
                """
                INSERT INTO rag.knowledge_bases
                  (tenant_id, name, description, chunking_strategy, chunk_size, chunk_overlap,
                   embedding_model_alias, embedding_model_resolved, embedding_dim)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    tenant_id, name, description, chunking_strategy, chunk_size, chunk_overlap,
                    embedding_model_alias, embedding_model_resolved, embedding_dim,
                ),
            )
        except UniqueViolation as exc:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"A knowledge base named '{name}' already exists.",
                status_code=409,
            ) from exc
        row = await cur.fetchone()
        assert row is not None
        kb_id = str(row["kb_id"])
        if private:
            # private: omit the tenant-wide default; grant the CREATOR full access so they
            # (and explicit ACL adds) can still reach the KB (Component 5c `private: true`).
            await conn.execute(
                """
                INSERT INTO rag.kb_acls
                  (kb_id, tenant_id, principal_type, principal_id, permissions, created_by, expires_at)
                VALUES (%s,%s,'agent',%s,%s,%s,NULL)
                ON CONFLICT (kb_id, principal_type, principal_id) DO NOTHING
                """,
                (kb_id, tenant_id, created_by, _DEFAULT_ACL_PERMS, created_by),
            )
        else:
            await conn.execute(
                """
                INSERT INTO rag.kb_acls
                  (kb_id, tenant_id, principal_type, principal_id, permissions, created_by)
                VALUES (%s,%s,'tenant','*',%s,%s)
                ON CONFLICT (kb_id, principal_type, principal_id) DO NOTHING
                """,
                (kb_id, tenant_id, _DEFAULT_ACL_PERMS, created_by),
            )
        return _kb_row_to_dict(row)

    return await in_tenant(pool, tenant_id, _txn)


async def get_kb(pool: AsyncConnectionPool, tenant_id: str, kb_id: str) -> dict[str, Any] | None:
    async def _txn(conn: AsyncConnection) -> dict[str, Any] | None:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM rag.knowledge_bases WHERE kb_id = %s", (kb_id,))
        row = await cur.fetchone()
        return _kb_row_to_dict(row) if row else None

    return await in_tenant(pool, tenant_id, _txn)


async def list_kbs(
    pool: AsyncConnectionPool, tenant_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    async def _txn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT * FROM rag.knowledge_bases ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [_kb_row_to_dict(r) for r in rows]

    return await in_tenant(pool, tenant_id, _txn)


async def delete_kb(pool: AsyncConnectionPool, tenant_id: str, kb_id: str) -> bool:
    async def _txn(conn: AsyncConnection) -> bool:
        cur = await conn.execute("DELETE FROM rag.knowledge_bases WHERE kb_id = %s", (kb_id,))
        return cur.rowcount > 0

    return await in_tenant(pool, tenant_id, _txn)


async def kb_status(pool: AsyncConnectionPool, tenant_id: str, kb_id: str) -> dict[str, Any]:
    async def _txn(conn: AsyncConnection) -> dict[str, Any]:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM rag.documents WHERE kb_id = %(kb)s) AS document_count,
              (SELECT COUNT(*) FROM rag.chunks    WHERE kb_id = %(kb)s) AS chunk_count,
              (SELECT COUNT(*) FROM rag.documents
                 WHERE kb_id = %(kb)s AND status IN ('pending','processing')) AS pending_docs,
              (SELECT COUNT(*) FROM rag.documents
                 WHERE kb_id = %(kb)s AND status = 'failed') AS failed_docs,
              (SELECT MAX(completed_at) FROM rag.documents
                 WHERE kb_id = %(kb)s) AS last_updated_at
            """,
            {"kb": kb_id},
        )
        row = await cur.fetchone()
        assert row is not None
        return {
            "kb_id": kb_id,
            "document_count": int(row["document_count"]),
            "chunk_count": int(row["chunk_count"]),
            "pending_docs": int(row["pending_docs"]),
            "failed_docs": int(row["failed_docs"]),
            "last_updated_at": _iso(row["last_updated_at"]),
        }

    return await in_tenant(pool, tenant_id, _txn)


# ── Documents ───────────────────────────────────────────────────────────────────
def _doc_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": str(row["doc_id"]),
        "kb_id": str(row["kb_id"]),
        "name": row["name"],
        "source_type": row["source_type"],
        "source_uri": row["source_uri"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "error_msg": row["error_msg"],
        "created_at": _iso(row["created_at"]),
        "completed_at": _iso(row["completed_at"]),
    }


async def create_document(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    kb_id: str,
    name: str,
    source_type: str,
    source_uri: str | None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
) -> dict[str, Any]:
    async def _txn(conn: AsyncConnection) -> dict[str, Any]:
        cur = conn.cursor(row_factory=dict_row)
        if doc_id:
            await cur.execute(
                """
                INSERT INTO rag.documents
                  (doc_id, kb_id, tenant_id, name, source_type, source_uri, status, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (doc_id, kb_id, tenant_id, name, source_type, source_uri, status, _jsonb(metadata)),
            )
        else:
            await cur.execute(
                """
                INSERT INTO rag.documents
                  (kb_id, tenant_id, name, source_type, source_uri, status, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (kb_id, tenant_id, name, source_type, source_uri, status, _jsonb(metadata)),
            )
        row = await cur.fetchone()
        assert row is not None
        return _doc_row_to_dict(row)

    return await in_tenant(pool, tenant_id, _txn)


async def get_document(
    pool: AsyncConnectionPool, tenant_id: str, doc_id: str
) -> dict[str, Any] | None:
    async def _txn(conn: AsyncConnection) -> dict[str, Any] | None:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT * FROM rag.documents WHERE doc_id = %s", (doc_id,))
        row = await cur.fetchone()
        return _doc_row_to_dict(row) if row else None

    return await in_tenant(pool, tenant_id, _txn)


async def list_documents(
    pool: AsyncConnectionPool, tenant_id: str, kb_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    async def _txn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT * FROM rag.documents WHERE kb_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (kb_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [_doc_row_to_dict(r) for r in rows]

    return await in_tenant(pool, tenant_id, _txn)


def _jsonb(value: dict[str, Any] | None) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value or {})
