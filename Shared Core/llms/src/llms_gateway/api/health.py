"""Health + metrics endpoints (Contract 7).

* ``GET /livez``   — process-only liveness; never touches DB/Valkey/Kafka/providers.
* ``GET /readyz``  — readiness gated on PostgreSQL connectivity (hard dependency);
  Valkey is SOFT — reported as ``ok | unavailable`` but never fails readiness;
  Kafka / providers are soft too (not checked here).
* ``GET /metrics`` — Prometheus exposition (version 0.0.4 text format).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.config import get_settings
from ..db.pool import readyz_ping

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
    checks: dict[str, str] = {}
    pool = getattr(request.app.state, "db_pool", None)
    db_ok = False
    if pool is not None:
        db_ok = await readyz_ping(pool)
    checks["postgresql"] = "ok" if db_ok else "fail"

    # Valkey is a SOFT dependency: report state (+ llms_valkey_up gauge via ping)
    # but NEVER gate readiness on it (Contract 7 / fail-open posture).
    valkey = getattr(request.app.state, "valkey", None)
    valkey_ok = await valkey.ping() if valkey is not None else False
    checks["valkey"] = "ok" if valkey_ok else "unavailable"

    ready = db_ok
    status_code = 200 if ready else 503
    return Response(
        content=_json({"ready": ready, "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _json(obj: dict[str, object]) -> str:
    import json

    return json.dumps(obj)
