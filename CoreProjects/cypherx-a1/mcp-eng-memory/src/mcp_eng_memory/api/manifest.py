"""GET /manifest — Contract-4 manifest with ETag / If-None-Match (304)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response

from ..core import metrics
from ..services import manifest as manifest_svc

router = APIRouter(tags=["mcp"])


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = [c.strip() for c in if_none_match.split(",")]
    if "*" in candidates:
        return True
    normalized = {c[2:] if c.startswith("W/") else c for c in candidates}
    return etag in normalized


@router.get("/manifest")
async def get_manifest(request: Request) -> Response:
    manifest = manifest_svc.build_manifest()
    etag = manifest_svc.manifest_etag(manifest)
    if _matches(request.headers.get("if-none-match"), etag):
        metrics.manifest_served_total.labels("304").inc()
        return Response(status_code=304, headers={"ETag": etag})
    metrics.manifest_served_total.labels("200").inc()
    return Response(
        content=json.dumps(manifest),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )
