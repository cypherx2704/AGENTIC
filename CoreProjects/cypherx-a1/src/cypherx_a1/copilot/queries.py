"""Read-only graph query tools — the structured, cited query surface.

These answer the core engineering-memory questions directly from the app-owned graph with
NO LLM call: ``who_owns`` / ``what_breaks_if_changed`` / ``experts_on`` / ``why_built`` /
``neighbors``. Each returns structured items + :class:`Citation` provenance. They back both
the public ``/v1/graph/*`` REST endpoints and the stateless ``mcp-eng-memory`` MCP server,
so an autonomous coding agent gets fast, deterministic, source-cited answers.

Every function runs read-only inside an ``in_tenant`` tx (RLS-scoped to the caller).
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..db import graph_repo
from ..db.pool import in_tenant
from ..models.api import Citation


def _entity_citation(row: dict[str, Any]) -> Citation:
    attrs = row.get("attrs") or {}
    return Citation(
        kind="entity",
        title=row.get("title") or row.get("natural_key") or "entity",
        source=row.get("source"),
        uri=attrs.get("url"),
        entity_id=str(row["entity_id"]) if row.get("entity_id") else None,
        entity_kind=row.get("kind"),
        natural_key=row.get("natural_key"),
    )


async def _resolve_target(conn: AsyncConnection, target: str, kinds: list[str] | None) -> dict | None:
    hits = await graph_repo.find_entities(conn, query=target, kinds=kinds, limit=1)
    return hits[0] if hits else None


class GraphQueryService:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def who_owns(self, *, tenant_id: str, target: str) -> tuple[list[dict], list[Citation]]:
        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            entity = await _resolve_target(conn, target, ["repo", "service", "feature", "document"])
            if entity is None:
                return [], []
            owners = await graph_repo.owners_of(conn, entity_id=str(entity["entity_id"]))
            items = [
                {
                    "person": o.get("title") or o.get("natural_key"),
                    "natural_key": o.get("natural_key"),
                    "relations": o.get("rels"),
                    "confidence": float(o.get("confidence") or 0.0),
                    "signal": int(o.get("signal") or 0),
                }
                for o in owners
            ]
            citations = [_entity_citation(entity)] + [_entity_citation(o) for o in owners]
            return items, citations

        return await in_tenant(self._pool, tenant_id, _q)

    async def what_breaks_if_changed(
        self, *, tenant_id: str, target: str, max_hops: int
    ) -> tuple[list[dict], list[Citation]]:
        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            entity = await _resolve_target(conn, target, ["service", "repo", "feature", "document"])
            if entity is None:
                return [], []
            impacted = await graph_repo.impact_of(conn, entity_id=str(entity["entity_id"]), max_hops=max_hops)
            items: list[dict] = []
            citations: list[Citation] = [_entity_citation(entity)]
            for imp in impacted:
                owners = await graph_repo.owners_of(conn, entity_id=str(imp["entity_id"]), limit=3)
                items.append(
                    {
                        "entity": imp.get("title") or imp.get("natural_key"),
                        "kind": imp.get("kind"),
                        "natural_key": imp.get("natural_key"),
                        "depth": int(imp.get("depth") or 1),
                        "owners": [o.get("title") or o.get("natural_key") for o in owners],
                    }
                )
                citations.append(_entity_citation(imp))
            return items, citations

        return await in_tenant(self._pool, tenant_id, _q)

    async def experts_on(self, *, tenant_id: str, topic: str) -> tuple[list[dict], list[Citation]]:
        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            experts = await graph_repo.experts_on(conn, topic=topic)
            items = [
                {
                    "person": e.get("title") or e.get("natural_key"),
                    "natural_key": e.get("natural_key"),
                    "relations": e.get("rels"),
                    "score": float(e.get("score") or 0.0),
                    "signal": int(e.get("signal") or 0),
                }
                for e in experts
            ]
            return items, [_entity_citation(e) for e in experts]

        return await in_tenant(self._pool, tenant_id, _q)

    async def why_built(self, *, tenant_id: str, feature: str) -> tuple[list[dict], list[Citation]]:
        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            hits = await graph_repo.find_entities(
                conn, query=feature, kinds=["pr", "feature", "decision", "ticket", "document"], limit=5
            )
            items = [
                {
                    "artifact": h.get("title") or h.get("natural_key"),
                    "kind": h.get("kind"),
                    "natural_key": h.get("natural_key"),
                    "url": (h.get("attrs") or {}).get("url"),
                }
                for h in hits
            ]
            return items, [_entity_citation(h) for h in hits]

        return await in_tenant(self._pool, tenant_id, _q)

    async def activity(
        self, *, tenant_id: str, target: str, since: str | None = None, until: str | None = None
    ) -> tuple[list[dict], list[Citation]]:
        """Phase B: a cited 'what changed, who worked on it, when' timeline for a repo or
        person, newest first."""

        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            entity = await _resolve_target(conn, target, None)
            if entity is None:
                return [], []
            rows = await graph_repo.activity_timeline(
                conn, scope_entity_id=str(entity["entity_id"]), since=since, until=until
            )
            items = [
                {
                    "activity": r.get("title"),
                    "kind": r.get("kind"),
                    "natural_key": r.get("natural_key"),
                    "author": r.get("author") or r.get("author_key"),
                    "when": r["occurred_at"].isoformat() if r.get("occurred_at") else None,
                    "via": r.get("via"),
                    "url": (r.get("attrs") or {}).get("url"),
                }
                for r in rows
            ]
            citations = [_entity_citation(entity)] + [_entity_citation(r) for r in rows]
            return items, citations

        return await in_tenant(self._pool, tenant_id, _q)

    async def neighbors(
        self, *, tenant_id: str, target: str, hops: int, as_of: str | None = None
    ) -> tuple[list[dict], list[Citation]]:
        """One-hop neighbours of ``target``. Phase KG: ``as_of`` (ISO 8601) time-travels to
        the edges that were valid at that instant; default (None) reads the current slice."""

        async def _q(conn: AsyncConnection) -> tuple[list[dict], list[Citation]]:
            entity = await _resolve_target(conn, target, None)
            if entity is None:
                return [], []
            if as_of:
                ns = await graph_repo.neighbors_as_of(
                    conn, entity_id=str(entity["entity_id"]), as_of=as_of, direction="both"
                )
            else:
                ns = await graph_repo.neighbors(conn, entity_id=str(entity["entity_id"]), direction="both")
            items = [
                {
                    "entity": n.get("title") or n.get("natural_key"),
                    "kind": n.get("kind"),
                    "natural_key": n.get("natural_key"),
                    "rel": n.get("rel"),
                    "confidence": float(n.get("confidence") or 0.0),
                }
                for n in ns
            ]
            citations = [_entity_citation(entity)] + [_entity_citation(n) for n in ns]
            return items, citations

        return await in_tenant(self._pool, tenant_id, _q)
