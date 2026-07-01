"""Connector registry — resolves a connector ``kind`` to its SPI implementation.

Adding a market source (Jira, Slack, Confluence, PagerDuty, CI/CD) is one entry here plus
one :class:`~cypherx_a1.connectors.base.Connector` subclass — the ingestion pipeline,
normalization, storage, retrieval, and copilot never change.
"""

from __future__ import annotations

from ..core.config import Settings
from .base import Connector
from .github import GitHubConnector

# kind -> factory(settings) -> Connector
_REGISTRY: dict[str, type[Connector]] = {
    "github": GitHubConnector,
    # "jira": JiraConnector,      # Phase 3
    # "slack": SlackConnector,    # Phase 3
}


def supported_kinds() -> list[str]:
    return sorted(_REGISTRY)


def get_connector(kind: str, settings: Settings) -> Connector:
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise KeyError(f"unknown connector kind: {kind}")
    return cls(settings)  # type: ignore[call-arg]
