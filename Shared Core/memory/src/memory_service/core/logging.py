"""Structured logging (Contract 6).

Configures structlog to emit JSON to stdout with the Contract 6 envelope fields:
``timestamp, level, service, version, environment, trace_id, span_id, request_id,
tenant_id, agent_id, message``. The correlation fields are merged in from structlog's
contextvars, which the trace middleware binds per request.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.types import EventDict, Processor

from .config import get_settings


def _add_service_fields(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """Inject the static service identity fields onto every record (Contract 6)."""
    settings = get_settings()
    event_dict["service"] = settings.service_name
    event_dict["version"] = settings.service_version
    event_dict["environment"] = settings.environment
    return event_dict


def _rename_event_to_message(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """Contract 6 names the human-readable field ``message`` (structlog uses ``event``)."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def configure_logging() -> None:
    """Configure structlog + stdlib logging to emit Contract 6 JSON to stdout."""
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_service_fields,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _rename_event_to_message,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
