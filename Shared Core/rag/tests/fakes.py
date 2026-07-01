"""In-memory fakes for the DB pool + Valkey so the suite needs no live infra.

``FakeDb`` is a tiny tabular store keyed by table name. ``FakeConnection.execute`` dispatches
the EXACT SQL the service issues (I wrote it all — the surface is bounded) to in-memory
operations, including the two-pass vector-search CTE (cosine similarity computed in Python).
Every ``SELECT set_config('app.tenant_id', ...)`` is recorded so RLS-isolation tests can
assert the tenant context AND so reads/writes are tenant-filtered exactly like real RLS.

This is deliberately not a general SQL engine — it answers the specific statements in
``db/repository.py``, ``services/store/pgvector.py``, ``services/acl.py``, ``services/quota.py``,
``api/*`` and ``worker/*``. If a new statement is added, extend the dispatcher.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return 1.0 - (dot / (na * nb))


def _parse_vector(literal: Any) -> list[float]:
    if isinstance(literal, list):
        return [float(x) for x in literal]
    s = str(literal).strip().lstrip("[").rstrip("]")
    return [float(x) for x in s.split(",") if x.strip()]


class FakeDb:
    """The shared in-memory data store (one per test)."""

    def __init__(self) -> None:
        self.knowledge_bases: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.chunks: list[dict[str, Any]] = []
        self.chunk_vectors_1536: list[dict[str, Any]] = []
        self.kb_acls: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.s3_deletions: list[dict[str, Any]] = []
        self.tenant_backends: list[dict[str, Any]] = []
        self.pricing: list[dict[str, Any]] = []
        # Every tenant context set via set_config, in order — RLS-isolation assertions.
        self.tenant_contexts: list[str] = []
        self.has_vector_ext = True

    # ── convenience accessors for tests ──────────────────────────────────────
    def outbox_topics(self) -> list[str]:
        return [r["topic"] for r in self.outbox]

    def outbox_payloads(self, topic: str) -> list[dict[str, Any]]:
        return [r["payload"]["payload"] for r in self.outbox if r["topic"] == topic]


class FakeCursor:
    def __init__(self, rows: list[Any], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._rows)


class _RowProxy(dict):
    """A dict that also supports integer indexing (tuple_row style)."""

    def __init__(self, mapping: dict[str, Any], order: list[str]) -> None:
        super().__init__(mapping)
        self._order = order

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)


class FakeConnection:
    def __init__(self, db: FakeDb, *, tenant: str | None = None) -> None:
        self._db = db
        # The tenant context is SHARED across the connection + any cursors derived from it
        # (psycopg cursors share the connection's session). A 1-element list is the shared cell.
        self._tenant_cell: list[str | None] = [tenant]
        self._row_factory: Any = None
        self._last: FakeCursor = FakeCursor([])

    @property
    def _tenant(self) -> str | None:
        return self._tenant_cell[0]

    @_tenant.setter
    def _tenant(self, value: str | None) -> None:
        self._tenant_cell[0] = value

    # psycopg-compatible surface ----------------------------------------------
    def cursor(self, row_factory: Any = None) -> FakeConnection:
        clone = FakeConnection(self._db)
        clone._tenant_cell = self._tenant_cell  # share the session tenant context
        clone._row_factory = row_factory
        return clone

    def transaction(self) -> _AsyncCtx:
        return _AsyncCtx(self)

    async def execute(self, sql: str, params: Any = None) -> FakeConnection:
        """Mirror psycopg: execute returns the cursor (self); rows fetched via fetch*."""
        self._last = await self._dispatch(sql, params)
        return self

    async def fetchone(self) -> Any:
        return await self._last.fetchone()

    async def fetchall(self) -> list[Any]:
        return await self._last.fetchall()

    @property
    def rowcount(self) -> int:
        return self._last.rowcount

    # internal dispatch -------------------------------------------------------
    async def _dispatch(self, sql: str, params: Any) -> FakeCursor:
        s = " ".join(sql.split())
        p = params or ()

        if "set_config('app.tenant_id'" in s:
            self._tenant = p[0]
            self._db.tenant_contexts.append(p[0])
            return FakeCursor([])
        if s.startswith("SET LOCAL hnsw.ef_search"):
            return FakeCursor([])
        if s.startswith("SELECT 1 FROM pg_extension"):
            return FakeCursor([(1,)] if self._db.has_vector_ext else [])
        if s == "SELECT 1":
            return FakeCursor([(1,)])

        # Two-pass CTE references chunk_vectors_1536 + chunks via aliases — match FIRST so the
        # table-substring loop below doesn't mis-route it to the plain vector handler.
        if "WITH candidates AS" in s:
            return await self._vector_search(s, p)
        # Hybrid (dense+lexical RRF) / sparse (lexical-only RRF) — match before the table loop.
        if "fused AS" in s:
            return await self._hybrid_search(s, p)

        for table, handler in (
            ("knowledge_bases", self._kb),
            ("kb_acls", self._acls),
            ("documents", self._documents),
            ("chunk_vectors_1536", self._vectors),  # before 'chunks' (substring)
            ("chunks", self._chunks),
            ("outbox", self._outbox),
            ("s3_deletions", self._s3_deletions),
            ("tenant_backends", self._tenant_backends),
        ):
            if f"rag.{table}" in s:
                return await handler(s, p)
        raise NotImplementedError(f"FakeConnection: unhandled SQL: {s[:120]}")

    def _tenant_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Emulate RLS: only rows for the current app.tenant_id are visible."""
        if self._tenant is None:
            return []
        return [r for r in rows if r.get("tenant_id") == self._tenant]

    def _wrap(self, mapping: dict[str, Any], order: list[str]) -> Any:
        if self._row_factory is not None:
            return _RowProxy(mapping, order)
        return _RowProxy(mapping, order)  # always support both index + key

    # ── knowledge_bases ──────────────────────────────────────────────────────
    async def _kb(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.knowledge_bases"):
            on_conflict = "ON CONFLICT" in s
            # Two INSERT shapes: the repository (9 cols) and the bootstrap (literal defaults).
            if "VALUES (%s,%s,%s,'sentence'" in s:  # bootstrap shape
                tenant_id, name, desc, model, dim = p
                strat, csize, cover, alias = "sentence", 512, 50, "embed"
            else:
                (tenant_id, name, desc, strat, csize, cover, alias, model, dim) = p
            existing = [
                r for r in db.knowledge_bases
                if r["tenant_id"] == tenant_id and r["name"] == name
            ]
            if existing:
                if on_conflict:
                    return FakeCursor([])
                from psycopg.errors import UniqueViolation

                raise UniqueViolation("duplicate kb name")
            row = {
                "kb_id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "name": name,
                "description": desc,
                "chunking_strategy": strat,
                "chunk_size": csize,
                "chunk_overlap": cover,
                "embedding_model_alias": alias,
                "embedding_model_resolved": model,
                "embedding_dim": dim,
                "status": "active",
                "created_at": _now(),
                "updated_at": _now(),
            }
            db.knowledge_bases.append(row)
            order = list(row.keys())
            return FakeCursor([self._wrap(row, order)])

        if s.startswith("SELECT kb_id FROM rag.knowledge_bases"):
            tenant_id, name = p
            for r in self._tenant_rows(db.knowledge_bases):
                if r["name"] == name:
                    return FakeCursor([self._wrap({"kb_id": r["kb_id"]}, ["kb_id"])])
            return FakeCursor([])

        if s.startswith("SELECT * FROM rag.knowledge_bases WHERE kb_id"):
            kb_id = p[0]
            for r in self._tenant_rows(db.knowledge_bases):
                if r["kb_id"] == kb_id:
                    return FakeCursor([self._wrap(r, list(r.keys()))])
            return FakeCursor([])

        if s.startswith("SELECT * FROM rag.knowledge_bases ORDER BY"):
            rows = sorted(
                self._tenant_rows(db.knowledge_bases),
                key=lambda r: r["created_at"], reverse=True,
            )
            limit, offset = p[-2], p[-1]
            sliced = rows[offset : offset + limit]
            return FakeCursor([self._wrap(r, list(r.keys())) for r in sliced])

        if s.startswith("SELECT COUNT(*) FROM rag.knowledge_bases"):
            return FakeCursor([(len(self._tenant_rows(db.knowledge_bases)),)])

        if s.startswith("DELETE FROM rag.knowledge_bases"):
            kb_id = p[0]
            before = len(db.knowledge_bases)
            keep_ids = {r["kb_id"] for r in self._tenant_rows(db.knowledge_bases) if r["kb_id"] == kb_id}
            db.knowledge_bases = [r for r in db.knowledge_bases if r["kb_id"] not in keep_ids]
            # Cascade documents/chunks/vectors/acls for the deleted kb.
            db.documents = [r for r in db.documents if r["kb_id"] != kb_id]
            db.chunks = [r for r in db.chunks if r["kb_id"] != kb_id]
            db.chunk_vectors_1536 = [r for r in db.chunk_vectors_1536 if r["kb_id"] != kb_id]
            db.kb_acls = [r for r in db.kb_acls if r["kb_id"] != kb_id]
            return FakeCursor([], rowcount=before - len(db.knowledge_bases))

        if "kb_id = %(kb)s) AS document_count" in s:  # kb_status aggregate
            kb = p["kb"] if isinstance(p, dict) else p[0]
            docs = [r for r in self._tenant_rows(db.documents) if r["kb_id"] == kb]
            chunks = [r for r in self._tenant_rows(db.chunks) if r["kb_id"] == kb]
            pending = [r for r in docs if r["status"] in ("pending", "processing")]
            failed = [r for r in docs if r["status"] == "failed"]
            completed_ats = [r["completed_at"] for r in docs if r.get("completed_at")]
            mapping = {
                "document_count": len(docs),
                "chunk_count": len(chunks),
                "pending_docs": len(pending),
                "failed_docs": len(failed),
                "last_updated_at": max(completed_ats) if completed_ats else None,
            }
            return FakeCursor([self._wrap(mapping, list(mapping.keys()))])

        raise NotImplementedError(f"kb SQL: {s[:120]}")

    # ── kb_acls ───────────────────────────────────────────────────────────────
    async def _acls(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.kb_acls"):
            if "'tenant','*'" in s:
                # repository default + bootstrap shape: (kb_id, tenant_id, perms, created_by)
                kb_id, tenant_id, perms, created_by = p
                ptype, pid, expires = "tenant", "*", None
            elif "VALUES (%s,%s,'agent'" in s:
                # private-KB creator grant: (kb_id, tenant_id, created_by(pid), perms, created_by)
                kb_id, tenant_id, pid, perms, created_by = p
                ptype, expires = "agent", None
            else:
                # add_acl / replace_acls: 7 positional params (expires_at last).
                kb_id, tenant_id, ptype, pid, perms, created_by = p[0], p[1], p[2], p[3], p[4], p[5]
                expires = p[6] if len(p) > 6 else None
            existing = [
                r for r in db.kb_acls
                if r["kb_id"] == kb_id and r["principal_type"] == ptype and r["principal_id"] == pid
            ]
            if existing:
                if "DO UPDATE" in s:
                    existing[0]["permissions"] = list(perms)
                    existing[0]["expires_at"] = expires
                return FakeCursor([])
            db.kb_acls.append({
                "kb_id": kb_id, "tenant_id": tenant_id, "principal_type": ptype,
                "principal_id": pid, "permissions": list(perms), "created_by": created_by,
                "created_at": _now(), "expires_at": expires,
            })
            return FakeCursor([])

        if s.startswith("SELECT principal_type, principal_id, permissions FROM rag.kb_acls"):
            kb_id, types = p[0], p[1]
            rows = [
                r for r in self._tenant_rows(db.kb_acls)
                if r["kb_id"] == kb_id and r["principal_type"] in types
                and (r["expires_at"] is None or r["expires_at"] > _now())
            ]
            wrapped = [
                self._wrap(
                    {"principal_type": r["principal_type"], "principal_id": r["principal_id"],
                     "permissions": r["permissions"]},
                    ["principal_type", "principal_id", "permissions"],
                )
                for r in rows
            ]
            return FakeCursor(wrapped)

        if s.startswith("SELECT * FROM rag.kb_acls"):
            kb_id = p[0]
            rows = [r for r in self._tenant_rows(db.kb_acls) if r["kb_id"] == kb_id]
            return FakeCursor([self._wrap(r, list(r.keys())) for r in rows])

        if s.startswith("DELETE FROM rag.kb_acls WHERE kb_id = %s AND principal_type"):
            kb_id, ptype, pid = p
            before = len(db.kb_acls)
            db.kb_acls = [
                r for r in db.kb_acls
                if not (r["kb_id"] == kb_id and r["principal_type"] == ptype and r["principal_id"] == pid)
            ]
            return FakeCursor([], rowcount=before - len(db.kb_acls))

        if s.startswith("DELETE FROM rag.kb_acls WHERE kb_id"):
            kb_id = p[0]
            db.kb_acls = [r for r in db.kb_acls if r["kb_id"] != kb_id]
            return FakeCursor([])

        raise NotImplementedError(f"acls SQL: {s[:120]}")

    # ── documents ─────────────────────────────────────────────────────────────
    async def _documents(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.documents"):
            with_doc_id = "(doc_id, kb_id" in s
            if with_doc_id:
                doc_id, kb_id, tenant_id, name, stype, suri, status, meta = p
            else:
                kb_id, tenant_id, name, stype, suri, status, meta = p
                doc_id = str(uuid.uuid4())
            row = {
                "doc_id": doc_id, "kb_id": kb_id, "tenant_id": tenant_id, "name": name,
                "source_type": stype, "source_uri": suri, "status": status, "attempts": 0,
                "error_msg": None, "metadata": dict(getattr(meta, "obj", {}) or {}),
                "created_at": _now(), "completed_at": None,
            }
            db.documents.append(row)
            return FakeCursor([self._wrap(row, list(row.keys()))])

        if s.startswith("SELECT * FROM rag.documents WHERE doc_id"):
            doc_id = p[0]
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    return FakeCursor([self._wrap(r, list(r.keys()))])
            return FakeCursor([])

        if s.startswith("SELECT * FROM rag.documents WHERE kb_id"):
            kb_id, limit, offset = p[0], p[1], p[2]
            rows = sorted(
                [r for r in self._tenant_rows(db.documents) if r["kb_id"] == kb_id],
                key=lambda r: r["created_at"], reverse=True,
            )[offset : offset + limit]
            return FakeCursor([self._wrap(r, list(r.keys())) for r in rows])

        if s.startswith("SELECT COUNT(*) FROM rag.documents WHERE kb_id"):
            kb_id = p[0]
            n = len([r for r in self._tenant_rows(db.documents) if r["kb_id"] == kb_id])
            return FakeCursor([(n,)])

        if s.startswith("UPDATE rag.documents SET status = 'completed'"):
            doc_id = p[0]
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    r["status"] = "completed"
                    r["completed_at"] = _now()
            return FakeCursor([])

        if s.startswith("UPDATE rag.documents SET status = 'processing'"):
            doc_id = p[0]
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    r["status"] = "processing"
            return FakeCursor([])

        if s.startswith("UPDATE rag.documents SET status = 'pending'"):
            doc_id = p[0]
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    r["status"] = "pending"
            return FakeCursor([])

        if s.startswith("UPDATE rag.documents SET status = 'failed'"):
            error, doc_id = p
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    r["status"] = "failed"
                    r["error_msg"] = error
            return FakeCursor([])

        if s.startswith("UPDATE rag.documents SET attempts = attempts + 1"):
            error, doc_id = p
            for r in self._tenant_rows(db.documents):
                if r["doc_id"] == doc_id:
                    r["attempts"] += 1
                    r["error_msg"] = error
                    return FakeCursor([self._wrap({"attempts": r["attempts"]}, ["attempts"])])
            return FakeCursor([])

        if s.startswith("DELETE FROM rag.documents"):
            doc_id = p[0]
            keep = {r["doc_id"] for r in self._tenant_rows(db.documents) if r["doc_id"] == doc_id}
            db.documents = [r for r in db.documents if r["doc_id"] not in keep]
            db.chunks = [r for r in db.chunks if r["doc_id"] != doc_id]
            db.chunk_vectors_1536 = [
                r for r in db.chunk_vectors_1536
                if r["chunk_id"] not in {c["chunk_id"] for c in db.chunks if c["doc_id"] == doc_id}
            ]
            return FakeCursor([], rowcount=len(keep))

        raise NotImplementedError(f"documents SQL: {s[:120]}")

    # ── chunks ────────────────────────────────────────────────────────────────
    async def _chunks(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("SELECT metadata->>'content_sha'"):
            doc_id = p[0]
            shas = [
                (r["metadata"].get("content_sha"),)
                for r in self._tenant_rows(db.chunks) if r["doc_id"] == doc_id
            ]
            return FakeCursor(shas)
        if s.startswith("INSERT INTO rag.chunks"):
            doc_id, kb_id, tenant_id, content, idx, model, dim, meta_json = p
            import json as _json

            chunk_id = str(uuid.uuid4())
            db.chunks.append({
                "chunk_id": chunk_id, "doc_id": doc_id, "kb_id": kb_id, "tenant_id": tenant_id,
                "content": content, "chunk_index": idx, "embedding_model": model,
                "embedding_dim": dim, "metadata": _json.loads(meta_json), "created_at": _now(),
            })
            return FakeCursor([(chunk_id,)])
        if s.startswith("SELECT COUNT(*) FROM rag.chunks WHERE kb_id"):
            kb_id = p[0]
            n = len([r for r in self._tenant_rows(db.chunks) if r["kb_id"] == kb_id])
            return FakeCursor([(n,)])
        if s.startswith("SELECT COUNT(*) FROM rag.chunks"):
            return FakeCursor([(len(self._tenant_rows(db.chunks)),)])
        raise NotImplementedError(f"chunks SQL: {s[:120]}")

    # ── chunk_vectors_1536 ──────────────────────────────────────────────────────
    async def _vectors(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.chunk_vectors_1536"):
            chunk_id, tenant_id, kb_id, embedding = p
            db.chunk_vectors_1536.append({
                "chunk_id": chunk_id, "tenant_id": tenant_id, "kb_id": kb_id,
                "embedding": _parse_vector(embedding),
            })
            return FakeCursor([])
        raise NotImplementedError(f"vectors SQL: {s[:120]}")

    async def _vector_search(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        vec = _parse_vector(p["vec"])
        kb_id = p["kb_id"]
        filters = p["filters"]
        min_score = p["min_score"]
        top_k = p["top_k"]
        import json as _json

        flt = _json.loads(filters) if filters else None

        chunks_by_id = {c["chunk_id"]: c for c in self._tenant_rows(db.chunks)}
        hits = []
        for cv in self._tenant_rows(db.chunk_vectors_1536):
            chunk = chunks_by_id.get(cv["chunk_id"])
            if chunk is None or chunk["kb_id"] != kb_id:
                continue
            if flt and not _contains(chunk["metadata"], flt):
                continue
            distance = _cosine_distance(vec, cv["embedding"])
            hits.append((distance, chunk))
        hits.sort(key=lambda h: h[0])
        results = []
        for distance, chunk in hits[: top_k * 2]:
            score = 1 - distance
            if score >= min_score:
                results.append(self._wrap(
                    {"chunk_id": chunk["chunk_id"], "content": chunk["content"],
                     "metadata": chunk["metadata"], "doc_id": chunk["doc_id"], "score": score},
                    ["chunk_id", "content", "metadata", "doc_id", "score"],
                ))
        return FakeCursor(results[:top_k])

    async def _hybrid_search(self, s: str, p: Any) -> FakeCursor:
        """Emulate the dense+lexical RRF (or sparse lexical-only RRF) fusion in Python.

        Mirrors services/store/pgvector.py search_hybrid: each leg ranks candidates, then the
        fused score = sum over legs of 1/(rrf_k + rank). 'sparse' runs the lexical leg only.
        The lexical leg approximates ts_rank_cd via token-overlap of the query against the
        chunk's content + the optional metadata['context'] prefix (weight 'A'/'B' folded in).
        """
        import json as _json

        db = self._db
        kb_id = p["kb_id"]
        filters = p.get("filters")
        rrf_k = p["rrf_k"]
        top_k = p["top_k"]
        candidates = p["candidates"]
        qtext = p["qtext"]
        flt = _json.loads(filters) if filters else None
        is_sparse = "dense AS" not in s

        chunks_by_id = {c["chunk_id"]: c for c in self._tenant_rows(db.chunks)}
        kb_chunks = [c for c in chunks_by_id.values() if c["kb_id"] == kb_id]
        if flt:
            kb_chunks = [c for c in kb_chunks if _contains(c["metadata"], flt)]

        rrf: dict[str, float] = {}

        # ── Lexical leg (always present) ───────────────────────────────────────
        q_tokens = {t for t in qtext.lower().split() if t}

        def _lex_score(chunk: dict) -> float:
            ctx = (chunk.get("metadata") or {}).get("context") or ""
            text = f"{ctx} {chunk['content']}".lower()
            d_tokens = set(text.split())
            return float(len(q_tokens & d_tokens))

        lexical = [(c, _lex_score(c)) for c in kb_chunks]
        lexical = [t for t in lexical if t[1] > 0]  # content_tsv @@ q (only matches)
        lexical.sort(key=lambda t: (t[1], t[0]["chunk_id"]), reverse=True)
        for rank, (chunk, _score) in enumerate(lexical[:candidates], start=1):
            rrf[chunk["chunk_id"]] = rrf.get(chunk["chunk_id"], 0.0) + 1.0 / (rrf_k + rank)

        # ── Dense leg (hybrid only) ────────────────────────────────────────────
        if not is_sparse:
            vec = _parse_vector(p["vec"])
            vec_by_chunk = {
                cv["chunk_id"]: cv["embedding"]
                for cv in self._tenant_rows(db.chunk_vectors_1536)
            }
            dense = []
            for c in kb_chunks:
                emb = vec_by_chunk.get(c["chunk_id"])
                if emb is None:
                    continue
                dense.append((c, _cosine_distance(vec, emb)))
            dense.sort(key=lambda t: t[1])
            for rank, (chunk, _dist) in enumerate(dense[:candidates], start=1):
                rrf[chunk["chunk_id"]] = rrf.get(chunk["chunk_id"], 0.0) + 1.0 / (rrf_k + rank)

        fused = sorted(rrf.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        results = []
        for chunk_id, score in fused[:top_k]:
            chunk = chunks_by_id[chunk_id]
            results.append(self._wrap(
                {"chunk_id": chunk_id, "content": chunk["content"],
                 "metadata": chunk["metadata"], "doc_id": chunk["doc_id"], "score": score},
                ["chunk_id", "content", "metadata", "doc_id", "score"],
            ))
        return FakeCursor(results)

    # ── outbox ────────────────────────────────────────────────────────────────
    async def _outbox(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.outbox"):
            topic, partition_key, payload = p
            db.outbox.append({
                "id": str(uuid.uuid4()), "topic": topic, "partition_key": partition_key,
                "payload": getattr(payload, "obj", payload), "created_at": _now(),
                "published_at": None, "attempts": 0,
            })
            return FakeCursor([])
        if s.startswith("SELECT id, topic, partition_key, payload, attempts FROM rag.outbox"):
            rows = [
                (r["id"], r["topic"], r["partition_key"], r["payload"], r["attempts"])
                for r in db.outbox if r["published_at"] is None
            ]
            return FakeCursor(rows[:100])
        if s.startswith("UPDATE rag.outbox SET published_at"):
            row_id = p[0]
            for r in db.outbox:
                if r["id"] == row_id:
                    r["published_at"] = _now()
            return FakeCursor([])
        raise NotImplementedError(f"outbox SQL: {s[:120]}")

    # ── s3_deletions ────────────────────────────────────────────────────────────
    async def _s3_deletions(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.s3_deletions"):
            doc_id, tenant_id, prefix = p
            if not any(r["doc_id"] == doc_id for r in db.s3_deletions):
                db.s3_deletions.append({
                    "doc_id": doc_id, "tenant_id": tenant_id, "s3_prefix": prefix,
                    "requested_at": _now(), "attempts": 0,
                })
            return FakeCursor([])
        if s.startswith("SELECT doc_id, s3_prefix FROM rag.s3_deletions"):
            rows = [
                self._wrap({"doc_id": r["doc_id"], "s3_prefix": r["s3_prefix"]},
                           ["doc_id", "s3_prefix"])
                for r in db.s3_deletions if r["attempts"] < 100
            ]
            return FakeCursor(rows)
        if s.startswith("DELETE FROM rag.s3_deletions"):
            doc_id = p[0]
            db.s3_deletions = [r for r in db.s3_deletions if r["doc_id"] != doc_id]
            return FakeCursor([])
        if s.startswith("UPDATE rag.s3_deletions SET attempts"):
            doc_id = p[0]
            for r in db.s3_deletions:
                if r["doc_id"] == doc_id:
                    r["attempts"] += 1
            return FakeCursor([])
        raise NotImplementedError(f"s3_deletions SQL: {s[:120]}")

    # ── tenant_backends ──────────────────────────────────────────────────────────
    async def _tenant_backends(self, s: str, p: Any) -> FakeCursor:
        db = self._db
        if s.startswith("INSERT INTO rag.tenant_backends"):
            tenant_id = p[0]
            if not any(r["tenant_id"] == tenant_id for r in db.tenant_backends):
                db.tenant_backends.append({"tenant_id": tenant_id, "backend_type": "pgvector"})
            return FakeCursor([])
        raise NotImplementedError(f"tenant_backends SQL: {s[:120]}")


def _contains(meta: dict, flt: dict) -> bool:
    """Emulate the @> jsonb containment operator (shallow)."""
    return all(meta.get(k) == v for k, v in flt.items())


class _AsyncCtx:
    """Async context manager for conn / conn.transaction()."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    """A minimal psycopg-pool stand-in over a shared FakeDb."""

    def __init__(self, db: FakeDb) -> None:
        self._db = db

    def connection(self, timeout: float | None = None) -> _AsyncCtx:
        return _AsyncCtx(FakeConnection(self._db))

    async def open(self, wait: bool = False) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeValkey:
    """In-memory Valkey: get/set/set_if_absent/incr_with_expire (no TTL semantics needed)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._store: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.fail = fail  # when True every op raises (simulate Valkey down)

    async def ping(self) -> bool:
        return not self.fail

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        if self.fail:
            raise RuntimeError("valkey down")
        return self._store.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds=None, timeout_seconds=None) -> None:
        if self.fail:
            raise RuntimeError("valkey down")
        self._store[key] = value

    async def set_if_absent(self, key: str, value: str, *, ttl_seconds, timeout_seconds=None) -> bool:
        if self.fail:
            raise RuntimeError("valkey down")
        if key in self._store:
            return False
        self._store[key] = value
        return True

    async def delete(self, key: str, *, timeout_seconds=None) -> None:
        if self.fail:
            raise RuntimeError("valkey down")
        self._store.pop(key, None)

    async def incr_with_expire(self, key: str, *, ttl_seconds, timeout_seconds=None) -> int:
        if self.fail:
            raise RuntimeError("valkey down")
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def close(self) -> None:
        return None


# Silence unused-import-style lint for the regex helper if a future statement needs it.
_ = re
