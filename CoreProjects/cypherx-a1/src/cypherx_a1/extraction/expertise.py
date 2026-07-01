"""Phase C — Degree-of-Knowledge expertise + ownership concentration (derived pass).

For each repo, tallies its contributors from the CURRENT graph — every person who authored
or (lower-weighted) reviewed an artifact (pr / ticket / change / incident) that is part_of /
touched the repo — with a **recency decay** so "who knows this code right now" is recency-
aware (Fritz 2010 Degree-of-Knowledge; Bird 2011 ownership; Caul 2020 review signal). It
writes a recency-decayed ``expert_in`` edge per (person, repo) and records the repo's
**ownership concentration** (Herfindahl over authorship shares → an effective-contributors /
bus-factor signal) on the repo's attrs.

Runs in the same trigger as consolidation (``/v1/extract?consolidate=true`` + the scheduled
worker tick). Idempotent: edges supersede-in-place, attrs merge. Pure graph + SQL — no LLM,
no new service.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..core.config import Settings
from ..db import graph_repo
from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)

_CONTRIBUTORS_SQL = """
    SELECT r.entity_id  AS repo_id,
           p.entity_id  AS person_id,
           ce.rel       AS contrib_rel,
           COALESCE((a.attrs->>'timestamp')::timestamptz, a.valid_from) AS occurred_at
      FROM cypherx_a1.entities r
      JOIN cypherx_a1.edges ae
        ON ae.dst_entity_id = r.entity_id AND ae.rel IN ('part_of','touched') AND ae.valid_to IS NULL
      JOIN cypherx_a1.entities a
        ON a.entity_id = ae.src_entity_id AND a.valid_to IS NULL
       AND a.kind IN ('pr','ticket','change','incident')
      JOIN cypherx_a1.edges ce
        ON ce.dst_entity_id = a.entity_id AND ce.rel IN ('authored','reviewed') AND ce.valid_to IS NULL
      JOIN cypherx_a1.entities p
        ON p.entity_id = ce.src_entity_id AND p.kind = 'person' AND p.valid_to IS NULL
     WHERE r.kind = 'repo' AND r.valid_to IS NULL
"""


@dataclass
class ExpertiseStats:
    repos: int = 0
    expert_edges: int = 0


def _decay(occurred_at: datetime | None, now: datetime, halflife: float) -> float:
    if occurred_at is None:
        return 0.5  # unknown time → mild, non-zero weight
    try:
        age = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
        return 0.5 ** (age / max(1.0, halflife))
    except (TypeError, ValueError):
        return 0.5


async def run_expertise_refresh(
    pool: AsyncConnectionPool, *, tenant_id: str, settings: Settings
) -> ExpertiseStats:
    stats = ExpertiseStats()
    now = datetime.now(UTC)
    hl = settings.expertise_recency_halflife_days
    rev_w = settings.expertise_reviewed_weight

    async def _read(conn: AsyncConnection) -> list[dict]:
        cur = await conn.cursor(row_factory=dict_row).execute(_CONTRIBUTORS_SQL)
        return [dict(r) for r in await cur.fetchall()]

    rows = await in_tenant(pool, tenant_id, _read)
    if not rows:
        return stats

    # Tally recency-decayed expertise score + authorship counts per (repo, person).
    score: dict[tuple[str, str], float] = defaultdict(float)
    authored: dict[tuple[str, str], int] = defaultdict(int)
    repos: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        rid, pid = str(r["repo_id"]), str(r["person_id"])
        w = 1.0 if r["contrib_rel"] == "authored" else rev_w
        score[(rid, pid)] += w * _decay(r.get("occurred_at"), now, hl)
        if r["contrib_rel"] == "authored":
            authored[(rid, pid)] += 1
        repos[rid].add(pid)

    async def _write(conn: AsyncConnection) -> int:
        written = 0
        for rid, persons in repos.items():
            scores = {pid: score[(rid, pid)] for pid in persons}
            top = max(scores.values()) or 1.0
            for pid, sc in scores.items():
                await graph_repo.upsert_edge(
                    conn, src_entity_id=pid, dst_entity_id=rid, rel="expert_in",
                    confidence=round(min(1.0, sc / top), 3),
                    extractor_version=settings.expertise_version,
                    metadata={"dok": True, "score": round(sc, 3)},
                )
                written += 1
            # Ownership concentration (Herfindahl over authorship shares) → bus-factor signal.
            total_auth = sum(authored[(rid, pid)] for pid in persons) or 1
            herfindahl = sum((authored[(rid, pid)] / total_auth) ** 2 for pid in persons)
            effective = round(1.0 / herfindahl, 2) if herfindahl else 0.0
            await conn.execute(
                "UPDATE cypherx_a1.entities SET attrs = attrs || %s "
                " WHERE entity_id = %s AND valid_to IS NULL",
                (Jsonb({"ownership_concentration": round(herfindahl, 3),
                        "effective_contributors": effective,
                        "bus_factor_risk": "high" if effective < 1.5 else "ok"}), rid),
            )
            stats.repos += 1
        return written

    stats.expert_edges = await in_tenant(pool, tenant_id, _write)
    return stats
