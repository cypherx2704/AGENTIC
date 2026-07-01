"""Health + metrics endpoints (Contract 7).

* ``GET /livez``   — process-only liveness; never touches Valkey/providers.
* ``GET /readyz``  — readiness. This server is stateless (no DB), so it is READY as soon
  as the process is up. Valkey is a SOFT dependency: reported as ``ok | unavailable`` but
  NEVER gates readiness (fail-open posture).
* ``GET /metrics`` — Prometheus exposition (version 0.0.4 text format).
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.config import get_settings

router = APIRouter(tags=["health"])

_START_TIME = time.monotonic()


@router.get("/livez")
async def livez() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": settings.service_version,
        "uptime_seconds": round(time.monotonic() - _START_TIME, 3),
    }


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    # No hard dependencies — the stateless MCP server is ready once the process is up.
    # Valkey is SOFT: report state (+ the tws_valkey_up gauge via ping) but never gate.
    valkey = getattr(request.app.state, "valkey", None)
    valkey_ok = await valkey.ping() if valkey is not None else False
    checks = {"valkey": "ok" if valkey_ok else "unavailable"}
    return Response(
        content=json.dumps({"ready": True, "checks": checks}),
        status_code=200,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
