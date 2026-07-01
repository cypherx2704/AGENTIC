"""Knowledge-extraction engine.

Reads engineering artifacts (PRs / tickets / incidents / decisions / docs) that have NOT
yet been extracted at the current ``extractor_version`` and asks the llms-gateway (with
``response_format=json_object``) to surface relationships the raw ingest can't see —
``depends_on`` / ``decided_in`` / ``caused`` / ``resolved`` / ``expert_in`` — each with a
confidence and evidence. Extracted edges are written with the current ``extractor_version``
so a model/prompt bump SUPERSEDES prior versions bitemporally rather than duplicating.

Discipline (idempotency + cost):
  * keyed by ``(tenant_id, node_id, content_sha, extractor_version)`` in
    ``extraction_jobs`` — re-ingest never re-spends.
  * each gateway chat call carries an ``Idempotency-Key`` so a retried worker replays.
  * the gateway's ``llm_call_id`` + ``cost_usd`` are recorded; cypherx-a1 never rewrites
    the gateway's cost numbers (Contract 19).

In keyless/mock-provider mode the gateway returns a canned completion (no useful JSON), so
extraction yields few/no edges and simply records the job — the explicit ingest edges
already answer the demo queries; extraction is a strict enrichment when a real provider is
configured.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import Settings
from ..db import graph_repo, ingest_repo
from ..db.pool import in_tenant
from ..kg import DEFAULT_SCHEMA, parse_extracted_edges
from ..kg.extraction import DEFAULT_EXTRACTABLE_RELS, DEFAULT_TARGET_KINDS
from ..services.llms_client import LlmsClient

logger = structlog.get_logger(__name__)

# Edge relations the extractor is allowed to emit (a subset of the graph vocabulary that an
# LLM can reasonably infer from artifact text). 'owns'/'authored'/'reviewed'/'part_of' come
# from deterministic ingest, not the LLM. Re-exported from the reusable kg lib (a future
# shared service is a lift-out, not a rewrite) — kept as module names for back-compat.
_EXTRACTABLE_RELS = set(DEFAULT_EXTRACTABLE_RELS)
_TARGET_KINDS = set(DEFAULT_TARGET_KINDS)

_SYSTEM_PROMPT = (
    "You extract a software-engineering knowledge graph from one artifact. "
    "Return STRICT JSON: {\"edges\": [{\"rel\": <one of "
    "depends_on|decided_in|caused|resolved|expert_in|mentions>, \"target_kind\": <one of "
    "service|repo|feature|decision|incident|person|document|pr|ticket>, \"target_key\": "
    "<stable natural key, e.g. a service name or 'owner/repo'>, \"confidence\": <0..1>, "
    "\"evidence\": <short quote>}]}. Only include edges strongly supported by the text. "
    "If none, return {\"edges\": []}."
)


@dataclass
class ExtractionStats:
    nodes_seen: int = 0
    nodes_extracted: int = 0
    edges_added: int = 0
    skipped: int = 0
    failed: int = 0


def _idem_key(tenant_id: str, node_id: str, content_sha: str, ev: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{node_id}:{content_sha}:{ev}".encode()).hexdigest()


async def run_extraction(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str,
    agent_id: str | None,
    llms: LlmsClient,
    settings: Settings,
    limit: int = 50,
) -> ExtractionStats:
    stats = ExtractionStats()
    ev = settings.extractor_version

    async def _list(conn: AsyncConnection) -> list[dict]:
        return await ingest_repo.list_unextracted_entities(conn, extractor_version=ev, limit=limit)

    pending = await in_tenant(pool, tenant_id, _list)
    for node in pending:
        stats.nodes_seen += 1
        try:
            added = await _extract_node(
                pool, tenant_id=tenant_id, agent_jwt=agent_jwt, agent_id=agent_id,
                llms=llms, settings=settings, node=node, ev=ev,
            )
            stats.nodes_extracted += 1
            stats.edges_added += added
            metrics.extraction_jobs_total.labels("completed").inc()
        except Exception as exc:  # noqa: BLE001 — one node's failure must not abort the pass
            stats.failed += 1
            metrics.extraction_jobs_total.labels("failed").inc()
            logger.warning("extraction_node_failed", node_id=node.get("entity_id"), error=str(exc))
    return stats


async def _extract_node(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str,
    agent_id: str | None,
    llms: LlmsClient,
    settings: Settings,
    node: dict,
    ev: str,
) -> int:
    node_id = str(node["entity_id"])
    content_sha = str(node["content_sha"])
    text = f"{node.get('title') or ''}\n\n{node.get('search_text') or ''}".strip()[:6000]

    completion = await llms.chat(
        model=settings.extraction_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Artifact ({node.get('kind')} {node.get('natural_key')}):\n{text}"},
        ],
        max_tokens=settings.extraction_max_tokens,
        temperature=settings.extraction_temperature,
        response_format={"type": "json_object"},
        agent_jwt=agent_jwt,
        on_behalf_of=agent_id,
        idempotency_key=_idem_key(tenant_id, node_id, content_sha, ev),
    )
    # Phase KG: schema-guided extraction is opt-in (default schema reproduces today's allowed
    # set). When off, schema=None ⇒ only the basic shape allow-list applies (unchanged).
    schema = DEFAULT_SCHEMA if settings.extraction_schema_enabled else None
    parsed = parse_extracted_edges(
        completion.content,
        floor=settings.extraction_confidence_floor,
        mode=settings.confidence_floor_mode,
        schema=schema,
        schema_mode=settings.extraction_schema_mode,
        source_kind=node.get("kind"),
    )
    edges = parsed.edges
    if parsed.rejected:
        metrics.extraction_edges_rejected_total.inc(parsed.rejected)

    capture_span = settings.extraction_span_capture_enabled

    async def _write(conn: AsyncConnection) -> int:
        await graph_repo.supersede_extracted_edges(conn, src_entity_id=node_id, extractor_version=ev)
        added = 0
        for e in edges:
            target_id = await graph_repo.get_entity_id(conn, kind=e.target_kind, natural_key=e.target_key)
            if target_id is None:
                target_id = await graph_repo.upsert_entity(
                    conn, kind=e.target_kind, source="extracted", natural_key=e.target_key,
                    title=e.target_key, search_text=e.target_key, external_id=None, attrs={},
                    content_sha=None,
                )
            meta: dict = {"evidence": e.evidence}
            if e.flagged:
                meta["flagged"] = True  # below the confidence floor; retained for recall
            if not e.schema_ok:
                meta["schema_ok"] = False  # off-schema, retained (schema_mode='flag')
            # Phase KG extraction QA: source span + the extractor's own confidence, recorded
            # only when span capture is enabled (today's path writes neither — no change).
            span = e.source_span if capture_span else None
            xconf = e.confidence if capture_span else None
            # upsert_extracted_edge records a supersedes_edge_id chain on a content change.
            await graph_repo.upsert_extracted_edge(
                conn, src_entity_id=node_id, dst_entity_id=target_id, rel=e.rel,
                confidence=e.confidence, extractor_version=ev, metadata=meta,
                source_span=span, extraction_confidence=xconf,
            )
            added += 1
        await ingest_repo.record_extraction_job(
            conn, node_id=node_id, content_sha=content_sha, extractor_version=ev,
            edges_extracted=added, llm_call_id=completion.llm_call_id, cost_usd=completion.usage.cost_usd,
        )
        return added

    return await in_tenant(pool, tenant_id, _write)


def _parse_edges(content: str | None, *, floor: float = 0.0, mode: str = "flag") -> list[dict]:
    """Parse + validate the LLM's JSON edges. Tolerant: a non-JSON / malformed response
    yields [] (the job is still recorded so it is not retried forever).

    Phase A confidence floor: an edge below ``floor`` is dropped (``mode='drop'``) or kept
    with ``flagged=True`` (``mode='flag'``, default — preserves recall, readers can filter).

    Thin backward-compatible wrapper over the reusable ``kg.parse_extracted_edges`` (returns
    the historical list-of-dict shape); the schema/span gates are applied in ``_extract_node``.
    """
    parsed = parse_extracted_edges(content, floor=floor, mode=mode)
    return [
        {"rel": e.rel, "target_kind": e.target_kind, "target_key": e.target_key,
         "confidence": e.confidence, "evidence": e.evidence, "flagged": e.flagged}
        for e in parsed.edges
    ]
