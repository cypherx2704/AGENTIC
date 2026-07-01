"""Live Phase C smoke (against a real Postgres): ingest fixtures, run the Degree-of-Knowledge
expertise refresh, and print the recency-decayed expert_in edges + ownership concentration.

    DATABASE_URL='postgresql://cxa1_user:pw@localhost:55434/cypherx_platform' \
      python scripts/verify_phase_c.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "demo")
DSN = os.environ["DATABASE_URL"]
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from cypherx_a1.connectors.github import _fixture_records  # noqa: E402
from cypherx_a1.core.config import get_settings  # noqa: E402
from cypherx_a1.db import pool as dbpool  # noqa: E402
from cypherx_a1.db.pool import in_tenant  # noqa: E402
from cypherx_a1.extraction.expertise import run_expertise_refresh  # noqa: E402
from cypherx_a1.ingestion.normalizer import upsert_graph  # noqa: E402

T = "00000000-0000-0000-0000-0000000000aa"


async def main() -> None:
    pool = dbpool.create_pool(DSN)
    await pool.open(wait=True, timeout=15)
    for rec in _fixture_records("commit"):
        async def _w(conn, rec=rec):  # noqa: ANN001, ANN202
            await upsert_graph(conn, rec)
        await in_tenant(pool, T, _w)

    st = await run_expertise_refresh(pool, tenant_id=T, settings=get_settings())
    print(f"expertise refresh: repos={st.repos}, expert_in edges written={st.expert_edges}")

    async def _q(conn):  # noqa: ANN001, ANN202
        ce = await (await conn.execute(
            "SELECT p.title, e.confidence, e.metadata->>'score' FROM cypherx_a1.edges e "
            "JOIN cypherx_a1.entities p ON p.entity_id=e.src_entity_id "
            "WHERE e.rel='expert_in' AND e.valid_to IS NULL AND (e.metadata->>'dok')='true' "
            "ORDER BY e.confidence DESC")).fetchall()
        cr = await (await conn.execute(
            "SELECT natural_key, attrs->>'effective_contributors', attrs->>'ownership_concentration', "
            "attrs->>'bus_factor_risk' FROM cypherx_a1.entities WHERE kind='repo' AND valid_to IS NULL")).fetchall()
        return ce, cr

    edges, repos = await in_tenant(pool, T, _q)
    print("\nexpert_in (Degree-of-Knowledge, recency-decayed):")
    for title, conf, score in edges:
        print(f"  {title}  conf={conf}  score={score}")
    print("\nownership concentration (bus-factor):")
    for nk, eff, herf, risk in repos:
        print(f"  {nk}: effective_contributors={eff}  herfindahl={herf}  risk={risk}")
    print(f"\n-> {'PASS' if st.expert_edges > 0 and edges else 'FAIL'}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
