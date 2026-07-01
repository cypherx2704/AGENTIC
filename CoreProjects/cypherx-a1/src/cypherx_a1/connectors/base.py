"""Connector SPI — the source-agnostic contract every connector implements.

A connector turns a source system (GitHub, Jira, Slack, …) into a stream of
:class:`~cypherx_a1.models.canonical.CanonicalRecord`. The ingestion pipeline is written
ONCE against this interface; adding a market tool is one SPI subclass + one registry row,
with zero changes to normalization, storage, retrieval, or the copilot.

Two acquisition modes, both app-owned (RAG has no push ingestion):
  * PULL — ``full_sync`` (backfill, resumable via an opaque cursor) and
    ``incremental_sync`` (scheduled delta).
  * PUSH — ``verify_signature`` + ``parse_webhook`` for the app-owned webhook receiver.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..models.canonical import CanonicalRecord


@dataclass
class SyncBatch:
    """One bounded page of a sync. ``next_cursor`` resumes the next page; ``done`` marks
    the backfill complete for this stream."""

    records: list[CanonicalRecord] = field(default_factory=list)
    next_cursor: str | None = None
    done: bool = True


class Connector(ABC):
    """Source connector contract. Stateless w.r.t. tenant — identity + cursors are passed
    in by the pipeline; the connector only knows how to talk to its source + normalize."""

    #: stable connector kind (matches ``connectors.kind`` and the registry key).
    kind: str = "base"

    @abstractmethod
    async def full_sync(self, *, stream: str, cursor: str | None) -> SyncBatch:
        """Resumable backfill of ``stream`` (e.g. ``repo:owner/name:pulls``) from ``cursor``."""

    @abstractmethod
    async def incremental_sync(self, *, stream: str, cursor: str | None) -> SyncBatch:
        """Delta sync of ``stream`` since ``cursor`` (defaults to full_sync semantics)."""

    @abstractmethod
    def verify_signature(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """Verify a webhook signature against the connector's shared secret."""

    @abstractmethod
    def parse_webhook(self, *, event: str, payload: dict) -> list[CanonicalRecord]:
        """Normalize one inbound webhook delivery into canonical records (may be empty)."""

    def streams(self) -> list[str]:
        """The list of streams this connector backfills (override per connector)."""
        return []
