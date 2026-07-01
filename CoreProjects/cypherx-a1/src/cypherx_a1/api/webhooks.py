"""App-owned webhook receiver — ``POST /webhooks/{kind}`` (RAG has no push ingestion).

Verifies the source signature (HMAC), normalizes the delivery into canonical records, and
lands + graph-normalizes them for the bound tenant. RAG embedding is DEFERRED on this path
(a webhook carries no agent JWT to forward to RAG — see the integration boundary doc);
an authenticated ``/v1/connectors/{kind}/sync`` or the worker embeds the landed records.

Tenant binding (MVP): the per-tenant webhook URL carries ``?tenant=<uuid>``; the HMAC
signature authenticates the payload. Production hardens this to a per-connector-install
path token (Phase 3). No platform JWT is required — the signature is the authenticator.
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Request, Response

from ..connectors.registry import get_connector, supported_kinds
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..ingestion.pipeline import ingest_records

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks"])

_EVENT_HEADERS = ("x-github-event", "x-event-key", "x-event")


@router.post("/webhooks/{kind}")
async def receive(kind: str, request: Request, tenant: str = "") -> Response:
    settings: Settings = request.app.state.settings
    if kind not in supported_kinds():
        raise ApiError(ErrorCode.NOT_FOUND, f"Unknown connector '{kind}'.")
    if not tenant:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Missing ?tenant binding for the webhook.")

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    connector = get_connector(kind, settings)
    try:
        if not connector.verify_signature(headers=headers, body=body):
            raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid webhook signature.")

        event = next((headers[h] for h in _EVENT_HEADERS if h in headers), "unknown")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Webhook body is not valid JSON.") from exc

        records = connector.parse_webhook(event=event, payload=payload)
        # Graph-only landing (no agent JWT -> no RAG embed on this path).
        stats = await ingest_records(
            request.app.state.db_pool,
            tenant_id=tenant,
            agent_jwt=None,
            agent_id=None,
            records=records,
            rag=None,
            kb_resolver=None,
            producer_version=settings.service_version,
        )
    finally:
        aclose = getattr(connector, "aclose", None)
        if aclose is not None:
            await aclose()

    return Response(
        content=json.dumps(
            {"accepted": True, "event": event, "records_new": stats.records_new,
             "nodes_upserted": stats.nodes_upserted, "note": "RAG embedding deferred (no agent JWT on webhook path)"}
        ),
        status_code=202,
        media_type="application/json",
    )
