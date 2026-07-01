"""Health + metrics endpoints (Contract 7).

* ``GET /livez``   — process-only liveness; never touches DB/classifier/Kafka.
* ``GET /readyz``  — readiness gated on PostgreSQL connectivity + a platform-default
  policy + classifier-ready + rules-registry consistency (a code/DB rule_id MISMATCH
  fails readiness — WP02 overlay). FIX D: in stub mode the classifier is ALWAYS ready,
  so readiness MUST NOT hard-fail on a missing detoxify model. Valkey is SOFT: its
  reachability is reported in ``checks`` but never fails readiness.
* ``GET /metrics`` — Prometheus exposition (version 0.0.4 text format).
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..core.config import get_settings
from ..db.pool import readyz_ping
from ..services.rules import registry as rules_registry

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

    # ── PostgreSQL (hard dependency) ─────────────────────────────────────────
    pool = getattr(request.app.state, "db_pool", None)
    db_ok = await readyz_ping(pool) if pool is not None else False
    checks["postgresql"] = "ok" if db_ok else "fail"

    # ── Platform-default policy (hard dependency; built-in stands in with no pool) ──
    policy_engine = getattr(request.app.state, "policy_engine", None)
    policy_ok = await policy_engine.has_platform_default() if policy_engine is not None else False
    checks["platform_default_policy"] = "ok" if policy_ok else "fail"

    # ── Classifier (FIX D: stub is ALWAYS ready) ──────────────────────────────
    classifier = getattr(request.app.state, "classifier", None)
    classifier_ok = bool(classifier.ready) if classifier is not None else False
    checks["classifier"] = "ok" if classifier_ok else "fail"

    # ── Rules registry overlay (hard ONLY on mismatch; 'unavailable' is soft —
    #    the postgresql check owns DB-down and the in-code defaults stand) ───────
    registry = getattr(request.app.state, "rule_registry", None)
    registry_status = registry.status if registry is not None else rules_registry.STATUS_OK
    checks["rules_registry"] = registry_status
    registry_ok = registry_status != rules_registry.STATUS_MISMATCH

    # ── Valkey (soft dependency: reported + gauged, NEVER fails readiness) ─────
    valkey = getattr(request.app.state, "valkey", None)
    if valkey is not None:
        checks["valkey"] = "ok" if await valkey.ping() else "unavailable"

    ready = db_ok and policy_ok and classifier_ok and registry_ok
    status_code = 200 if ready else 503
    return Response(
        content=json.dumps({"ready": ready, "checks": checks}),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
