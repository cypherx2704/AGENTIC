"""Reflection / consolidation pass (Generative-Agents memory win, Park et al. 2023).

Turns the accreting graph into *active knowledge*: it clusters each person's current
contribution edges (authored / reviewed / owns / expert_in), and for high-confidence
clusters synthesizes a short **expertise summary** via the llms-gateway, writing a new
``expertise_summary`` entity + ``summarizes`` evidence edges (``source='consolidation'``).

Discipline (mirrors the extractor):
  * **idempotent + cost-metered** via an ``extraction_jobs`` row keyed by the summary node +
    a content_sha of the cluster + ``consolidation_version`` — an unchanged cluster is
    skipped (no re-spend); a changed cluster supersedes the prior summary in place (the
    entity keeps its id; edges supersede-in-place), never duplicating.
  * **graph-only** — the summary is NOT embedded into RAG (honors the invariant).
  * keyless/mock-safe — if the gateway returns no usable JSON, a deterministic fallback
    summary is written so the pass still produces a node offline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import Settings
from ..db import graph_repo, ingest_repo
from ..db.pool import in_tenant
from ..services.llms_client import LlmsClient

logger = structlog.get_logger(__name__)

_CLUSTER_RELS = ("authored", "reviewed", "owns", "expert_in")
_SYSTEM_PROMPT = (
    "You summarize a software engineer's area of expertise from the titles of the artifacts "
    "they authored/reviewed/own. Return STRICT JSON: {\"summary\": <one or two sentences>, "
    "\"topics\": [<short topic strings>]}. Be concrete and grounded in the titles."
)


@dataclass
class ConsolidationStats:
    persons_seen: int = 0
    summaries_written: int = 0
    skipped: int = 0
    failed: int = 0


def _cluster_sha(target_ids: list, version: str) -> str:
    ids = ",".join(sorted(str(i) for i in (target_ids or [])))
    return hashlib.sha256(f"{ids}:{version}".encode()).hexdigest()


async def run_consolidation(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str,
    agent_id: str | None,
    llms: LlmsClient,
    settings: Settings,
) -> ConsolidationStats:
    stats = ConsolidationStats()
    ver = settings.consolidation_version

    async def _clusters(conn: AsyncConnection) -> list[dict]:
        return await graph_repo.consolidation_clusters(
            conn, rels=_CLUSTER_RELS, min_cluster=settings.consolidation_min_cluster,
            limit=settings.consolidation_lookback_limit,
        )

    clusters = await in_tenant(pool, tenant_id, _clusters)
    for c in clusters:
        stats.persons_seen += 1
        avg = float(c.get("avg_conf") or 0.0)
        if avg < settings.consolidation_avg_confidence:
            stats.skipped += 1
            continue
        try:
            written = await _consolidate_one(
                pool, tenant_id=tenant_id, agent_jwt=agent_jwt, agent_id=agent_id,
                llms=llms, settings=settings, cluster=c, ver=ver,
            )
            if written:
                stats.summaries_written += 1
                metrics.extraction_jobs_total.labels("completed").inc()
            else:
                stats.skipped += 1
        except Exception as exc:  # noqa: BLE001 — one person's failure must not abort the pass
            stats.failed += 1
            metrics.extraction_jobs_total.labels("failed").inc()
            logger.warning("consolidation_person_failed", person=c.get("person_key"), error=str(exc))
    return stats


async def _consolidate_one(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str,
    agent_id: str | None,
    llms: LlmsClient,
    settings: Settings,
    cluster: dict,
    ver: str,
) -> bool:
    person_key = str(cluster["person_key"])
    person_title = cluster.get("person_title") or person_key
    target_ids = [str(t) for t in (cluster.get("target_ids") or []) if t]
    titles = [t for t in (cluster.get("target_titles") or []) if t]
    summary_nk = f"expertise:{person_key}"
    content_sha = _cluster_sha(target_ids, ver)

    # Idempotency: skip if this exact cluster was already consolidated at this version.
    async def _seen(conn: AsyncConnection) -> tuple[bool, str | None]:
        sid = await graph_repo.get_entity_id(conn, kind="expertise_summary", natural_key=summary_nk)
        if sid is None:
            return False, None
        done = await ingest_repo.extraction_job_done(
            conn, node_id=sid, content_sha=content_sha, extractor_version=ver
        )
        return done, sid

    already, _ = await in_tenant(pool, tenant_id, _seen)
    if already:
        return False

    # Synthesize (LLM, json_object) with a deterministic mock-safe fallback.
    summary_text, topics, llm_call_id, cost = await _synthesize(
        llms, person_title, titles, settings, agent_jwt, agent_id, content_sha
    )

    async def _write(conn: AsyncConnection) -> None:
        person_id = await graph_repo.get_entity_id(conn, kind="person", natural_key=person_key)
        sid = await graph_repo.upsert_entity(
            conn, kind="expertise_summary", source="consolidation", natural_key=summary_nk,
            title=f"Expertise: {person_title}",
            search_text=f"{person_title} expertise: {summary_text} {' '.join(topics)}",
            external_id=None, attrs={"person": person_key, "topics": topics, "summary": summary_text,
                                     "evidence_count": len(target_ids)},
            content_sha=content_sha,
        )
        # summarizes -> the subject person + the evidence artifacts.
        if person_id:
            await graph_repo.upsert_edge(
                conn, src_entity_id=sid, dst_entity_id=person_id, rel="summarizes",
                confidence=1.0, extractor_version=ver, metadata={"role": "subject"},
            )
        for tid in target_ids:
            await graph_repo.upsert_edge(
                conn, src_entity_id=sid, dst_entity_id=tid, rel="summarizes",
                confidence=1.0, extractor_version=ver, metadata={"role": "evidence"},
            )
        await ingest_repo.record_extraction_job(
            conn, node_id=sid, content_sha=content_sha, extractor_version=ver,
            edges_extracted=len(target_ids) + (1 if person_id else 0),
            llm_call_id=llm_call_id, cost_usd=cost,
        )

    await in_tenant(pool, tenant_id, _write)
    return True


async def _synthesize(
    llms: LlmsClient, person_title: str, titles: list[str], settings: Settings,
    agent_jwt: str, agent_id: str | None, content_sha: str,
) -> tuple[str, list[str], str | None, float]:
    bullets = "\n".join(f"- {t}" for t in titles[:8])
    try:
        completion = await llms.chat(
            model=settings.extraction_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Engineer: {person_title}\nArtifacts:\n{bullets}"},
            ],
            max_tokens=settings.consolidation_max_tokens, temperature=0.2,
            response_format={"type": "json_object"}, agent_jwt=agent_jwt, on_behalf_of=agent_id,
            idempotency_key=f"consolidate:{content_sha}",
        )
        data = json.loads(completion.content or "{}")
        summary = str(data.get("summary", "")).strip()
        topics = [str(t) for t in (data.get("topics") or []) if t][:8]
        if summary:
            return summary, topics, completion.llm_call_id, completion.usage.cost_usd
        llm_call_id, cost = completion.llm_call_id, completion.usage.cost_usd
    except Exception as exc:  # noqa: BLE001 — fall back deterministically (keyless/mock)
        logger.info("consolidation_llm_fallback", error=str(exc))
        llm_call_id, cost = None, 0.0
    # Deterministic fallback so the pass produces a node even offline.
    summary = f"Active across {len(titles)} artifacts: " + "; ".join(titles[:5])
    return summary, titles[:5], llm_call_id, cost
