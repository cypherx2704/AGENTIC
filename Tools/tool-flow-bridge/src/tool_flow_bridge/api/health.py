"""Health + metrics endpoints (Contract 7).

* ``GET /livez``   — process-only liveness; never touches Postgres/Valkey.
* ``GET /readyz``  — readiness gated on Postgres (the bridge's hard dependency). Valkey is
  SOFT: reported as ``ok | unavailable`` but never gates.
* ``GET /metrics`` — Prometheus exposition (0.0.4 text format).
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.config import get_settings
from ..db import pool as db_pool

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
    pool = getattr(request.app.state, "db_pool", None)
    db_ok = await db_pool.readyz_ping(pool) if pool is not None else False

    valkey = getattr(request.app.state, "valkey", None)
    valkey_ok = await valkey.ping() if valkey is not None else False

    ready = db_ok  # Postgres is the only hard dependency; Valkey is soft.
    checks = {
        "postgres": "ok" if db_ok else "unavailable",
        "valkey": "ok" if valkey_ok else "unavailable",
    }
    return Response(
        content=json.dumps({"ready": ready, "checks": checks}),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
