"""Billing-replay journal (WP05, best-effort, optional-infra).

When the post-completion DB usage write fails (``api/chat._write_usage`` returns
``billing_pending=True``), the in-flight :class:`~llms_gateway.db.outbox.UsageWrite`
is appended here as ONE JSON line (NDJSON) to a local append-only file. A replay
worker (or ``/readyz``, or an admin hook) can later call :func:`replay_pending` to
re-drive the journalled records into Postgres once the DB is reachable again.

**Fail-open posture (same as the rest of WP05):** a journal *write* failure only
logs + counts a metric — it must never affect the response the client already
received (they were already charged tokens). The provider already returned; the
worst case is that a burned token is dropped from the durable replay path, which
is exactly the failure mode the outbox + journal exist to make rare.

**Why append-only NDJSON, not the DB:** the journal is the fallback for *when the
DB is unreachable*, so it cannot itself depend on the DB. One ``UsageWrite`` per
line keeps appends atomic-enough (a single ``write()`` of a line) and lets the
replay reader skip a single corrupt/torn trailing line without losing the rest.

Public API:

    await append(write, settings=...)              # never raises (logs on failure)
    n = await replay_pending(pool, settings=...)    # re-drive journalled records

Both are no-ops when ``settings.billing_journal_enabled`` is false.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings
from ..db.outbox import UsageWrite, record_usage

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


def _journal_path(settings: Settings) -> Path:
    return Path(settings.billing_journal_path)


def _append_sync(path: Path, line: str) -> None:
    """Blocking append of one NDJSON line (run in a thread). Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode + single write of a newline-terminated line so concurrent
    # appenders don't interleave (POSIX append writes under the line size are atomic).
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


async def append(write: UsageWrite, *, settings: Settings | None = None) -> None:
    """Append ``write`` to the billing-replay journal. NEVER raises.

    Serialises the :class:`UsageWrite` dataclass to a single JSON line. A failure to
    write the journal is swallowed (logged + ``billing_journal_failed_total``): the
    response the client got is already sent, so journaling must be strictly best-effort.

    Args:
        write: the usage record whose DB persist just failed.
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.billing_journal_enabled:
        return
    try:
        line = json.dumps(dataclasses.asdict(write), separators=(",", ":"))
        await asyncio.to_thread(_append_sync, _journal_path(settings), line)
        metrics.billing_journal_appended_total.inc()
        logger.warning(
            "billing_journal_appended",
            llm_call_id=write.llm_call_id,
            tenant_id=write.tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — journaling is best-effort, never affects the response
        metrics.billing_journal_failed_total.inc()
        logger.error("billing_journal_append_failed", error=str(exc), llm_call_id=write.llm_call_id)


def _read_all_sync(path: Path) -> list[str]:
    """Read every line of the journal, or ``[]`` if it doesn't exist (blocking)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [ln for ln in (line.strip() for line in fh) if ln]


def _rewrite_sync(path: Path, lines: list[str]) -> None:
    """Atomically replace the journal with ``lines`` (empty -> remove the file)."""
    if not lines:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".billing-journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def replay_pending(
    pool: AsyncConnectionPool | None,
    *,
    settings: Settings | None = None,
) -> int:
    """Re-drive journalled :class:`UsageWrite` records into Postgres. Returns the count replayed.

    Reads the journal, attempts ``record_usage`` for each line (the gateway-minted
    ``llm_call_id`` makes this idempotent against a partial earlier success — a
    duplicate raises a unique violation, which we treat as already-billed and drop),
    then rewrites the journal with only the records that still failed. NEVER raises;
    on any unexpected error it logs and returns ``0`` so a worker loop keeps running.

    No-op (returns ``0``) when the journal is disabled or ``pool is None`` (no DB to
    replay into — leave the journal intact for a later run).

    Args:
        pool: the app DB pool (``request.app.state.db_pool``); ``None`` -> skip.
        settings: app settings; defaults to ``get_settings()``.
    """
    settings = settings or get_settings()
    if not settings.billing_journal_enabled or pool is None:
        return 0

    path = _journal_path(settings)
    try:
        lines = await asyncio.to_thread(_read_all_sync, path)
    except Exception as exc:  # noqa: BLE001 — never raise from a background worker
        logger.error("billing_journal_read_failed", error=str(exc))
        return 0
    if not lines:
        return 0

    still_pending: list[str] = []
    replayed = 0
    for line in lines:
        try:
            data = json.loads(line)
            write = UsageWrite(**{k: data[k] for k in data if k in _USAGE_WRITE_FIELDS})
        except (ValueError, TypeError) as exc:  # corrupt/torn line — drop it, do not block replay
            logger.warning("billing_journal_line_skipped", error=str(exc))
            continue
        try:
            await record_usage(pool, write, producer_version=settings.service_version)
            replayed += 1
            metrics.billing_journal_replayed_total.labels("replayed").inc()
        except Exception as exc:  # noqa: BLE001 — distinguish "already billed" from "still down"
            if _is_duplicate(exc):
                # A prior run already billed this llm_call_id (UNIQUE violation) — drop it.
                replayed += 1
                metrics.billing_journal_replayed_total.labels("replayed").inc()
                logger.info("billing_journal_duplicate_dropped", llm_call_id=write.llm_call_id)
            else:
                still_pending.append(line)
                metrics.billing_journal_replayed_total.labels("failed").inc()
                logger.warning("billing_journal_replay_failed", error=str(exc))

    try:
        await asyncio.to_thread(_rewrite_sync, path, still_pending)
    except Exception as exc:  # noqa: BLE001 — couldn't truncate; records may double-replay (idempotent)
        logger.error("billing_journal_rewrite_failed", error=str(exc))

    if replayed:
        logger.info("billing_journal_replayed", replayed=replayed, still_pending=len(still_pending))
    return replayed


# Field set used to filter unknown keys on deserialize (forward/backward compat).
_USAGE_WRITE_FIELDS = frozenset(f.name for f in dataclasses.fields(UsageWrite))


def _is_duplicate(exc: Exception) -> bool:
    """True if ``exc`` is (or wraps) a Postgres UNIQUE violation for an already-billed call."""
    # psycopg raises psycopg.errors.UniqueViolation (sqlstate 23505). Avoid importing
    # psycopg at module import time (kept lazy elsewhere); match on the sqlstate attr/text.
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate == "23505":
        return True
    return "23505" in str(exc) or "unique" in str(exc).lower()
