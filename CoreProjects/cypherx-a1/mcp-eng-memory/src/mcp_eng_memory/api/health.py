"""Health + metrics (Contract 7). /livez process-only; /readyz gates on JWKS warm."""

from __future__ import annotations

import json
import time

import structlog
from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.auth import get_jwks_client
from ..core.config import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])
_START = time.monotonic()


@router.get("/livez")
async def livez() -> dict[str, object]:
    return {"status": "ok", "version": get_settings().service_version,
            "uptime_seconds": round(time.monotonic() - _START, 3)}


@router.get("/readyz")
async def readyz(_request: Request) -> Response:
    settings = get_settings()
    checks: dict[str, str] = {}
    try:
        jwks_ok = bool(get_jwks_client(settings.auth_jwks_url).get_signing_keys())
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        logger.warning("jwks_ready_check_failed", error=str(exc))
        jwks_ok = False
    checks["auth_jwks"] = "ok" if jwks_ok else "fail"
    return Response(
        content=json.dumps({"ready": jwks_ok, "checks": checks}),
        status_code=200 if jwks_ok else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
