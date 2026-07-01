"""Structured logging (Contract 6) — JSON to stdout."""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.types import EventDict, Processor

from .config import get_settings


def _service_fields(_l: object, _m: str, ev: EventDict) -> EventDict:
    s = get_settings()
    ev["service"] = s.service_name
    ev["version"] = s.service_version
    ev["environment"] = s.environment
    return ev


def _event_to_message(_l: object, _m: str, ev: EventDict) -> EventDict:
    if "event" in ev:
        ev["message"] = ev.pop("event")
    return ev


def configure_logging() -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _service_fields,
        structlog.processors.format_exc_info,
        _event_to_message,
    ]
    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(), foreign_pre_chain=shared
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
