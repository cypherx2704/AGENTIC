"""Health + metrics endpoints (Contract 7).

* ``GET /livez``   — process-only liveness; never touches DB/Valkey/Kafka/S3/llms.
* ``GET /readyz``  — readiness gated on the HARD deps: PostgreSQL reachable, the pgvector
  extension present, AND the platform-skills bootstrap LOOP running (Component 10 —
  KB-row existence is NOT a gate, and NO live llms call is required). Valkey / Kafka / S3 /
  llms are SOFT (reported, never gate).
* ``GET /metrics`` — Prometheus exposition.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.config import get_settings
from ..db.pool import pgvector_present, readyz_ping

router = APIRouter(tags=["health"])

_START_TIME = time.monotonic()


@router.get("/livez")
async def livez() -> dict[str, object]:
    return {
        "status": "ok",
        "version": get_settings().service_version,
        "uptime_seconds": round(time.monotonic() - _START_TIME, 3),
    }


@router.get("/readyz")
async def readyz(request: Request) -> Response:
    checks: dict[str, str] = {}
    pool = getattr(request.app.state, "db_pool", None)

    db_ok = await readyz_ping(pool) if pool is not None else False
    checks["postgresql"] = "ok" if db_ok else "fail"

    # pgvector extension is a HARD dep (vector ops are the service). Only meaningful once
    # the DB is reachable.
    pgv_ok = await pgvector_present(pool) if (pool is not None and db_ok) else False
    checks["pgvector"] = "ok" if pgv_ok else "fail"

    # Component 10: readiness requires only that the bootstrap LOOP is running.
    bootstrap = getattr(request.app.state, "bootstrap", None)
    bootstrap_ok = bool(bootstrap.running) if bootstrap is not None else False
    checks["bootstrap_loop"] = "running" if bootstrap_ok else "not_running"

    # Soft deps — reported only.
    valkey = getattr(request.app.state, "valkey", None)
    valkey_ok = await valkey.ping() if valkey is not None else False
    checks["valkey"] = "ok" if valkey_ok else "unavailable"

    ready = db_ok and pgv_ok and bootstrap_ok
    status_code = 200 if ready else 503
    return Response(
        content=json.dumps({"ready": ready, "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
