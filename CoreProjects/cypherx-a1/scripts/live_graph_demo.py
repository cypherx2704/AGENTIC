"""Live backend-flow demo (graph path) against a real Postgres, as the RLS-enforced role.

Exercises the ACTUAL cypherx-a1 code — the connector fixtures, the normalizer (graph
upsert + cross-tool identity resolution), and the GraphQueryService (recursive-CTE
who_owns / what_breaks_if_changed / experts_on / why_built) — plus a cross-tenant RLS
check. The RAG/LLM legs are skipped (this proves the graph + RLS + query algorithms end to
end without upstream service tokens).

Run against the throwaway pg started for testing:
    DATABASE_URL='postgresql://cxa1_user:cxapw@localhost:55432/cypherx_platform' \
      python scripts/live_graph_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "demo")
DSN = os.environ.get(
    "DATABASE_URL", "postgresql://cxa1_user:cxapw@localhost:55432/cypherx_platform"
)
os.environ["DATABASE_URL"] = DSN

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from cypherx_a1.connectors.github import _fixture_records  # noqa: E402
from cypherx_a1.copilot.queries import GraphQueryService  # noqa: E402
from cypherx_a1.core.config import get_settings  # noqa: E402
from cypherx_a1.db import graph_repo  # noqa: E402
from cypherx_a1.db import pool as dbpool  # noqa: E402
from cypherx_a1.db.pool import in_tenant  # noqa: E402
from cypherx_a1.extraction.consolidator import run_consolidation  # noqa: E402
from cypherx_a1.ingestion.normalizer import upsert_graph  # noqa: E402
from cypherx_a1.services.llms_client import LlmsClient  # noqa: E402
from cypherx_a1.services.service_token import ServiceTokenProvider  # noqa: E402

TENANT_A = os.environ.get("DEMO_TENANT_A", "00000000-0000-0000-0000-0000000000aa")
TENANT_B = "00000000-0000-0000-0000-0000000000bb"


async def _count(conn) -> tuple[int, int]:  # noqa: ANN001
    e = (await (await conn.execute("SELECT count(*) FROM cypherx_a1.entities")).fetchone())[0]
    g = (await (await conn.execute("SELECT count(*) FROM cypherx_a1.edges")).fetchone())[0]
    return e, g


async def main() -> None:
    pool = dbpool.create_pool(DSN)
    await pool.open(wait=True, timeout=15)

    # ── Ingest the GitHub fixtures into tenant A's graph (real normalizer, graph-only) ──
    records = _fixture_records()
    edges = 0
    for rec in records:
        async def _w(conn, rec=rec):  # noqa: ANN001, ANN202
            res = await upsert_graph(conn, rec)
            return res.edges_upserted

        edges += await in_tenant(pool, TENANT_A, _w)
    ent, edg = await in_tenant(pool, TENANT_A, _count)
    print(f"INGESTED {len(records)} fixture records -> tenant A graph: {ent} entities, {edg} edges "
          f"({edges} edge upserts)")

    gq = GraphQueryService(pool)

    print("\n=== who_owns('acme/payments') ===")
    items, cits = await gq.who_owns(tenant_id=TENANT_A, target="acme/payments")
    for it in items:
        print(f"  {it['person']}  via {it['relations']}  (confidence {it['confidence']})")
    print(f"  citations: {len(cits)}")

    print("\n=== what_breaks_if_changed('auth-service', max_hops=3) ===")
    items, cits = await gq.what_breaks_if_changed(tenant_id=TENANT_A, target="auth-service", max_hops=3)
    for it in items:
        print(f"  {it['entity']} ({it['kind']}) depth={it['depth']} owners={it['owners']}")
    print(f"  citations: {len(cits)}")

    print("\n=== experts_on('payment retry') ===")
    items, _ = await gq.experts_on(tenant_id=TENANT_A, topic="payment retry")
    for it in items:
        print(f"  {it['person']}  score={it['score']}  via {it['relations']}")

    print("\n=== why_built('Stripe webhook') ===")
    items, _ = await gq.why_built(tenant_id=TENANT_A, feature="Stripe webhook")
    for it in items:
        print(f"  {it['artifact']} ({it['kind']})  {it.get('url')}")

    # ── Cross-tenant RLS: tenant B must see NONE of tenant A's data ──
    cb_ent, cb_edg = await in_tenant(pool, TENANT_B, _count)
    print(f"\n=== RLS cross-tenant check: tenant B sees {cb_ent} entities / {cb_edg} edges "
          f"(expected 0 / 0) -> {'PASS' if (cb_ent, cb_edg) == (0, 0) else 'FAIL'} ===")

    # ── Phase A: explicit supersede chain on a content change ──
    async def _supersede(conn) -> tuple[bool, bool]:  # noqa: ANN001
        alice = await graph_repo.get_entity_id(conn, kind="person", natural_key="alice@acme.io")
        svc = await graph_repo.get_entity_id(conn, kind="service", natural_key="auth-service")
        e1 = await graph_repo.upsert_extracted_edge(conn, src_entity_id=alice, dst_entity_id=svc,
                                                     rel="expert_in", confidence=0.6, extractor_version="demo")
        e2 = await graph_repo.upsert_extracted_edge(conn, src_entity_id=alice, dst_entity_id=svc,
                                                     rel="expert_in", confidence=0.95, extractor_version="demo")
        r = await (await conn.execute(
            "SELECT supersedes_edge_id FROM cypherx_a1.edges WHERE edge_id=%s", (e2,))).fetchone()
        old = await (await conn.execute(
            "SELECT valid_to FROM cypherx_a1.edges WHERE edge_id=%s", (e1,))).fetchone()
        return (str(r[0]) == e1), (old[0] is not None)

    links, closed = await in_tenant(pool, TENANT_A, _supersede)
    print(f"\n=== Phase A supersede chain: new edge -> old link={links}, old edge closed={closed} "
          f"-> {'PASS' if links and closed else 'FAIL'} ===")

    # ── Phase KG: bi-temporal history + entity resolution (merge + edge redirect) ──
    async def _kg(conn) -> tuple[bool, bool, int]:  # noqa: ANN001
        alice = await graph_repo.get_entity_id(conn, kind="person", natural_key="alice@acme.io")
        svc = await graph_repo.get_entity_id(conn, kind="service", natural_key="auth-service")
        # (1) Bi-temporal edge history for (alice -> auth-service, expert_in): the supersede
        # above produced a closed 0.6 edge + a current 0.95 edge — the chain is auditable.
        hist = await graph_repo.edge_history(
            conn, src_entity_id=alice, dst_entity_id=svc, rel="expert_in")
        history_ok = len(hist) >= 2 and any(h["invalidated_at"] is not None for h in hist)
        # (2) Entity resolution: create a duplicate person, redirect its edges to the
        # canonical, then merge. The duplicate's edges must move to alice.
        dup = await graph_repo.upsert_entity(
            conn, kind="person", source="demo", natural_key="a.ng@acme.io",
            title="A. Ng", search_text="A. Ng", external_id=None, attrs={}, content_sha=None)
        await graph_repo.upsert_edge(
            conn, src_entity_id=dup, dst_entity_id=svc, rel="expert_in",
            confidence=0.7, extractor_version="ingest")
        moved = await graph_repo.merge_entity(conn, loser_entity_id=dup, canonical_entity_id=alice)
        # The duplicate entity is bi-temporally closed (valid_to set); its edges redirected.
        row = await (await conn.execute(
            "SELECT valid_to FROM cypherx_a1.entities WHERE entity_id=%s", (dup,))).fetchone()
        dup_closed = row is not None and row[0] is not None
        return history_ok, dup_closed, moved

    history_ok, dup_closed, moved = await in_tenant(pool, TENANT_A, _kg)
    print(f"\n=== Phase KG bi-temporal history: chain audit={history_ok} "
          f"-> {'PASS' if history_ok else 'FAIL'} ===")
    print(f"=== Phase KG entity resolution: merged dup (edges redirected={moved}, "
          f"loser closed={dup_closed}) -> {'PASS' if dup_closed and moved >= 1 else 'FAIL'} ===")

    # ── Phase B: activity timeline (what changed, who, when) ──
    async def _activity(conn) -> list:  # noqa: ANN001
        repo = await graph_repo.get_entity_id(conn, kind="repo", natural_key="acme/payments")
        return await graph_repo.activity_timeline(conn, scope_entity_id=repo, limit=10)

    acts = await in_tenant(pool, TENANT_A, _activity)
    print(f"\n=== Phase B activity timeline for acme/payments ({len(acts)} events) ===")
    for a in acts[:6]:
        print(f"  [{a.get('occurred_at')}] {a.get('kind'):>7}: {a.get('title')}  by {a.get('author') or '?'}")

    # ── Phase B: consolidation (reflection) — idempotent ──
    settings = get_settings()
    llms = LlmsClient(settings, ServiceTokenProvider(settings))
    c1 = await run_consolidation(pool, tenant_id=TENANT_A, agent_jwt="", agent_id=None, llms=llms, settings=settings)
    c2 = await run_consolidation(pool, tenant_id=TENANT_A, agent_jwt="", agent_id=None, llms=llms, settings=settings)
    await llms.aclose()

    async def _summaries(conn) -> list:  # noqa: ANN001
        cur = await conn.execute(
            "SELECT title, attrs->>'summary' FROM cypherx_a1.entities "
            "WHERE kind='expertise_summary' AND valid_to IS NULL ORDER BY title")
        return await cur.fetchall()

    sums = await in_tenant(pool, TENANT_A, _summaries)
    print(f"\n=== Phase B consolidation: run1 wrote {c1.summaries_written}, run2 wrote {c2.summaries_written} "
          f"(idempotent -> {'PASS' if c2.summaries_written == 0 else 'FAIL'}) ===")
    for t, s in sums:
        print(f"  {t}: {(s or '')[:90]}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
