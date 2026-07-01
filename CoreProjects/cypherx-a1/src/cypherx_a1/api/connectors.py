"""Connector control surface — trigger a backfill/sync and the extraction pass.

  * ``POST /v1/connectors/{kind}/sync``    — pull from the source (mock fixtures by default;
    live GitHub when configured), normalize into the graph, embed docs into RAG. Resumable
    via ``sync_cursors``; idempotent via ``raw_events`` content_sha dedup.
  * ``POST /v1/extract``                   — run the LLM knowledge-extraction pass over
    not-yet-extracted artifacts (idempotent + cost-metered).

Both require the ingest scope. In a production deployment these are also driven by the
webhook receiver + a scheduled worker; the endpoints make the path explicit and testable.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request

from ..connectors.registry import get_connector, supported_kinds
from ..core.auth import Principal, ingest_scopes, require_principal, require_scope
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db import ingest_repo
from ..db.pool import in_tenant
from ..extraction.consolidator import run_consolidation
from ..extraction.expertise import run_expertise_refresh
from ..extraction.extractor import run_extraction
from ..ingestion.pipeline import ingest_records
from ..models.api import ExtractResponse, SyncRequest, SyncResponse

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["connectors"])


@router.post("/v1/connectors/{kind}/sync", response_model=SyncResponse)
async def sync(
    kind: str,
    body: SyncRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> SyncResponse:
    require_scope(principal, ingest_scopes(), "connector:sync")
    settings: Settings = request.app.state.settings
    pool = request.app.state.db_pool
    rag = request.app.state.rag_client
    kb_resolver = request.app.state.kb_resolver

    if kind not in supported_kinds():
        raise ApiError(ErrorCode.NOT_FOUND, f"Unknown connector '{kind}'. Supported: {supported_kinds()}.")
    connector = get_connector(kind, settings)

    display = body.repo or kind

    async def _ensure(conn) -> str:  # noqa: ANN001
        return await ingest_repo.get_or_create_connector(
            conn, kind=kind, display_name=display, config={"repo": body.repo} if body.repo else {}
        )

    connector_id = await in_tenant(pool, principal.tenant_id, _ensure)

    records = []
    try:
        for stream in connector.streams():
            seed = body.repo if settings.connector_mode == "live" else None

            async def _cur(conn, _s: str = stream) -> str | None:  # noqa: ANN001
                return await ingest_repo.get_cursor(conn, connector_id=connector_id, stream=_s)

            cursor = await in_tenant(pool, principal.tenant_id, _cur) or seed
            if body.mode == "incremental":
                batch = await connector.incremental_sync(stream=stream, cursor=cursor)
            else:
                batch = await connector.full_sync(stream=stream, cursor=cursor)
            records.extend(batch.records)
            if batch.next_cursor:
                async def _set(conn, _s: str = stream, _c: str = batch.next_cursor) -> None:  # noqa: ANN001
                    await ingest_repo.set_cursor(conn, connector_id=connector_id, stream=_s, cursor=_c)

                await in_tenant(pool, principal.tenant_id, _set)
    finally:
        aclose = getattr(connector, "aclose", None)
        if aclose is not None:
            await aclose()

    stats = await ingest_records(
        pool,
        tenant_id=principal.tenant_id,
        agent_jwt=principal.raw_token,
        agent_id=principal.agent_id,
        records=records,
        rag=rag,
        kb_resolver=kb_resolver,
        producer_version=settings.service_version,
        settings=settings,
    )
    return SyncResponse(
        connector=kind,
        records_seen=stats.records_seen,
        records_new=stats.records_new,
        nodes_upserted=stats.nodes_upserted,
        edges_upserted=stats.edges_upserted,
        docs_ingested=stats.docs_ingested,
        skipped_duplicate=stats.skipped_duplicate,
        errors=stats.errors,
    )


@router.post("/v1/extract", response_model=ExtractResponse)
async def extract(
    request: Request,
    principal: Principal = Depends(require_principal),
    consolidate: bool = False,
) -> ExtractResponse:
    """Run the LLM knowledge-extraction pass. With ``?consolidate=true`` (Phase B) it ALSO
    runs the reflection/consolidation pass that synthesizes expertise_summary nodes."""
    require_scope(principal, ingest_scopes(), "extract:run")
    settings: Settings = request.app.state.settings
    stats = await run_extraction(
        request.app.state.db_pool,
        tenant_id=principal.tenant_id,
        agent_jwt=principal.raw_token,
        agent_id=principal.agent_id,
        llms=request.app.state.llms_client,
        settings=settings,
    )
    summaries = persons = expert_edges = 0
    if consolidate:
        cstats = await run_consolidation(
            request.app.state.db_pool,
            tenant_id=principal.tenant_id,
            agent_jwt=principal.raw_token,
            agent_id=principal.agent_id,
            llms=request.app.state.llms_client,
            settings=settings,
        )
        summaries, persons = cstats.summaries_written, cstats.persons_seen
        # Phase C: recency-decayed Degree-of-Knowledge expert_in + ownership concentration.
        estats = await run_expertise_refresh(
            request.app.state.db_pool, tenant_id=principal.tenant_id, settings=settings
        )
        expert_edges = estats.expert_edges
    return ExtractResponse(
        nodes_seen=stats.nodes_seen,
        nodes_extracted=stats.nodes_extracted,
        edges_added=stats.edges_added,
        failed=stats.failed,
        summaries_written=summaries,
        persons_consolidated=persons,
        expert_edges_written=expert_edges,
    )
