"""Health + metrics endpoints (Contract 7).

Mirrors the llms-gateway health router.

  * ``GET /livez``   — process-only liveness; never touches DB/Kafka/downstream services.
  * ``GET /readyz``  — readiness gated on PostgreSQL connectivity + a warm Auth JWKS
    (both hard dependencies per the Phase 9 K8s spec). Kafka, Valkey and the downstream
    LLMs / Guardrails services are SOFT dependencies and are deliberately NOT gated on
    here (their outages are handled fail-soft on the task path), so a transient
    downstream outage must never flip xAgent un-ready. Valkey is soft-REPORTED in the
    checks map (and via the ``xagent_valkey_up`` gauge) without affecting ``ready``.
  * ``GET /metrics`` — Prometheus exposition (version 0.0.4 text format).
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

    # ── PostgreSQL (hard dependency) ─────────────────────────────────────────────
    pool = getattr(request.app.state, "db_pool", None)
    db_ok = await readyz_ping(pool) if pool is not None else False
    checks["postgresql"] = "ok" if db_ok else "fail"

    # ── Auth JWKS warm (hard dependency — needed to verify inbound agent JWTs) ────
    jwks_ok = _jwks_ready(settings.auth_jwks_url)
    checks["auth_jwks"] = "ok" if jwks_ok else "fail"

    # ── Valkey (SOFT dependency — reported + gauged, NEVER gates readiness) ───────
    valkey = getattr(request.app.state, "valkey", None)
    if valkey is not None:
        checks["valkey"] = "ok" if await valkey.ping() else "fail"

    ready = db_ok and jwks_ok
    status_code = 200 if ready else 503
    return Response(
        content=json.dumps({"ready": ready, "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _jwks_ready(jwks_url: str) -> bool:
    """Return True if the Auth JWKS document is fetchable (signing keys resolvable)."""
    try:
        keys = get_jwks_client(jwks_url).get_signing_keys()
        return bool(keys)
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        logger.warning("jwks_ready_check_failed", error=str(exc))
        return False
