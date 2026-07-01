"""Health + metrics endpoints (Contract 7).

  * ``GET /livez``   — process-only liveness; never touches DB/Kafka/downstream services.
  * ``GET /readyz``  — gated on PostgreSQL connectivity + a warm Auth JWKS (the two hard
    dependencies). Kafka/Valkey/downstream SharedCore services are SOFT (handled fail-soft
    on the request path) and never gate readiness; Valkey is soft-reported only.
  * ``GET /metrics`` — Prometheus exposition. Mirrors the xAgent ax-1 health router.
"""

from __future__ import annotations

import json
import time

import structlog
from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.auth import get_jwks_client
from ..core.config import get_settings
from ..db.pool import readyz_ping

logger = structlog.get_logger(__name__)

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
    settings = get_settings()
    checks: dict[str, str] = {}

    pool = getattr(request.app.state, "db_pool", None)
    db_ok = await readyz_ping(pool) if pool is not None else False
    checks["postgresql"] = "ok" if db_ok else "fail"

    jwks_ok = _jwks_ready(settings.auth_jwks_url)
    checks["auth_jwks"] = "ok" if jwks_ok else "fail"

    valkey = getattr(request.app.state, "valkey", None)
    if valkey is not None:
        checks["valkey"] = "ok" if await valkey.ping() else "fail"

    ready = db_ok and jwks_ok
    return Response(
        content=json.dumps({"ready": ready, "checks": checks}),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _jwks_ready(jwks_url: str) -> bool:
    try:
        keys = get_jwks_client(jwks_url).get_signing_keys()
        return bool(keys)
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        logger.warning("jwks_ready_check_failed", error=str(exc))
        return False
