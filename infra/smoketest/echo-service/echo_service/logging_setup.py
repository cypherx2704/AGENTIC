"""Contract 6 structured JSON logging.

Every log line is a single-line JSON object with the exact field set from
contracts/logging/log-format.schema.json. No plain-text logs. Promtail parses
these with `| json` and Loki keeps the low-cardinality fields (service, level,
environment) as labels while tenant_id / trace_id / request_id / agent_id stay
queryable JSON fields only (Component 13 — they are NOT labels).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from typing import Any

from .config import settings

# Contract 6 allowed top-level keys. Anything service-specific goes under `extra`.
_STD_KEYS = {
    "timestamp",
    "level",
    "service",
    "version",
    "environment",
    "trace_id",
    "span_id",
    "request_id",
    "tenant_id",
    "agent_id",
    "message",
    "duration_ms",
}

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}


def _now() -> str:
    # RFC 3339 UTC, millisecond precision, trailing Z (Contract 6).
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def log(level: str, message: str, **fields: Any) -> None:
    """Emit one Contract 6 JSON log line to stdout.

    Recognised Contract 6 fields are placed at the top level; everything else is
    nested under `extra` so the schema's known-field set stays stable while
    arbitrary structured context is still captured.
    """
    level = level.upper()
    if _LEVELS.get(level, 20) < _LEVELS.get(settings.log_level, 20):
        return

    record: dict[str, Any] = {
        "timestamp": _now(),
        "level": level,
        "service": settings.service,
        "version": settings.version,
        "environment": settings.environment,
        "message": message,
    }

    extra: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in _STD_KEYS:
            record[key] = value
        else:
            extra[key] = value
    if extra:
        record["extra"] = extra

    # Single line, stdout, flush so K8s/Promtail picks it up immediately.
    sys.stdout.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()
