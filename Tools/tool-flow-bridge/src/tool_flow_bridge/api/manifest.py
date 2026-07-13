"""GET /w/{slug}/manifest — Contract-4 MCP manifest for a published workflow-tool.

UNAUTHENTICATED (like every MCP server's ``/manifest``) — the Tool Registry health-poller
fetches it with ``If-None-Match`` for 304. The manifest is REGENERATED deterministically from
the stored binding row on every read (a live projection, not a frozen snapshot), so a
platform-side manifest change propagates automatically via the registry's ETag poll — no
re-publish, no tool-version churn. Resolved by the workflow's globally-unique ``slug`` in
platform context (empty GUC), and carries a strong content-addressed ETag.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Request, Response

from ..core import metrics
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..db import pool as db_pool
from ..db import queries
from ..services import manifest_builder

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


def _manifest_response(manifest: dict, if_none_match: str | None) -> Response:
    """ETag-conditional response shared by the single-tool and aggregating manifest endpoints."""
    etag = _etag(manifest)
    if _matches(if_none_match, etag):
        metrics.manifest_served_total.labels("304").inc()
        return Response(status_code=304, headers={"ETag": etag})
    metrics.manifest_served_total.labels("200").inc()
    return Response(
        content=json.dumps(manifest),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@router.get("/w/{slug}/manifest")
async def get_manifest(slug: str, request: Request) -> Response:
    """Legacy single-tool manifest. Back-compat alias resolving from the new source-of-truth model:
    a flow-tool's slug is ALSO its singleton MCP's slug, so this returns the aggregating projection
    (name = the singleton's ``tool-<slug>`` server_name, base_url = the canonical ``/m/<slug>`` wire).
    Mirrors GET /m/{mcp_slug}/manifest."""
    pool = request.app.state.db_pool

    async def _load(conn):
        return await queries.get_mcp_with_members(conn, slug)

    loaded = await db_pool.in_platform(pool, _load)
    if loaded is None or loaded[0]["status"] != "active":
        raise ApiError(ErrorCode.NOT_FOUND, f"No published tool for slug '{slug}'.")

    mcp_row, members = loaded
    # Regenerate from the persisted rows (deterministic projection), so platform-side manifest
    # changes surface via the ETag without a re-publish.
    manifest = manifest_builder.mcp_manifest_from_row(get_settings(), mcp_row, members)
    return _manifest_response(manifest, request.headers.get("if-none-match"))


@router.get("/m/{mcp_slug}/manifest")
async def get_mcp_manifest(mcp_slug: str, request: Request) -> Response:
    """UNAUTHENTICATED Contract-4 manifest for an MCP (aggregating server). Resolved by the MCP's
    globally-unique slug in platform context (empty GUC) and regenerated from the persisted mcp +
    member-tool rows on every read — a live projection, so a generator change surfaces via the
    ETag with no re-publish. Mirrors GET /w/{slug}/manifest."""
    pool = request.app.state.db_pool

    async def _load(conn):
        return await queries.get_mcp_with_members(conn, mcp_slug)

    loaded = await db_pool.in_platform(pool, _load)
    if loaded is None or loaded[0]["status"] != "active":
        raise ApiError(ErrorCode.NOT_FOUND, f"No published MCP for slug '{mcp_slug}'.")

    mcp_row, members = loaded
    manifest = manifest_builder.mcp_manifest_from_row(get_settings(), mcp_row, members)
    return _manifest_response(manifest, request.headers.get("if-none-match"))
