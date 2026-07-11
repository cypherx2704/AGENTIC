"""GET /w/{slug}/manifest — Contract-4 MCP manifest for a published workflow-tool.

UNAUTHENTICATED (like every MCP server's ``/manifest``) — the Tool Registry health-poller
fetches it with ``If-None-Match`` for 304. The manifest is the exact object stored at
publish time; it is resolved by the workflow's globally-unique ``slug`` in platform
context (empty GUC), and carries a strong content-addressed ETag.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Request, Response

from ..core import metrics
from ..core.errors import ApiError, ErrorCode
from ..db import pool as db_pool
from ..db import queries

router = APIRouter(tags=["mcp"])


def _canonical(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def _etag(manifest: dict) -> str:
    return f'"{hashlib.sha256(_canonical(manifest).encode()).hexdigest()}"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = [c.strip() for c in if_none_match.split(",")]
    if "*" in candidates:
        return True
    normalized = {c[2:] if c.startswith("W/") else c for c in candidates}
    return etag in normalized


@router.get("/w/{slug}/manifest")
async def get_manifest(slug: str, request: Request) -> Response:
    pool = request.app.state.db_pool

    async def _load(conn):
        return await queries.get_binding_by_slug(conn, slug)

    binding = await db_pool.in_platform(pool, _load)
    if binding is None or binding["status"] != "active":
        raise ApiError(ErrorCode.NOT_FOUND, f"No published tool for slug '{slug}'.")

    manifest = binding["manifest"]
    etag = _etag(manifest)

    if _matches(request.headers.get("if-none-match"), etag):
        metrics.manifest_served_total.labels("304").inc()
        return Response(status_code=304, headers={"ETag": etag})

    metrics.manifest_served_total.labels("200").inc()
    return Response(
        content=json.dumps(manifest),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )
