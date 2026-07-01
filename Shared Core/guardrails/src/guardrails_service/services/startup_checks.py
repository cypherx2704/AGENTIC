"""Startup fail-fast checks (WP07).

Currently one check: **stream-mode support**. The first-cycle pipeline only supports the
``buffer`` stream mode (rules + policies carry ``stream_mode``/``default_stream_mode`` and
``buffer`` is the only implemented mode). If an ACTIVE policy or a platform rule requires a
stream mode the running build cannot service, that is a configuration error that would
silently mis-handle traffic — so the service FAILS FAST at startup rather than booting into
a state where streamed checks degrade incorrectly.

Fail-fast scope is intentionally narrow:
  * Only ``stream_mode`` values OUTSIDE the supported set trip it.
  * With no DB pool (local/unit) there is nothing to read and the check passes (the
    in-code rules are all ``buffer``).
  * A DB READ error does NOT fail fast (that is the readiness probe's job — a transient DB
    blip at boot must not crash the pod); only a successfully-read UNSUPPORTED mode does.
"""

from __future__ import annotations

import structlog
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

# The only stream mode the first-cycle pipeline implements.
SUPPORTED_STREAM_MODES: frozenset[str] = frozenset({"buffer"})


class UnsupportedStreamModeError(RuntimeError):
    """Raised at startup when an active policy/rule needs an unsupported stream mode."""


async def assert_supported_stream_modes(pool: AsyncConnectionPool | None) -> None:
    """Fail fast if any active policy/rule requires a stream mode we cannot support.

    No-op when no pool is configured. A DB-read failure is logged + tolerated (readiness
    owns DB connectivity); only a confirmed unsupported mode raises.
    """
    if pool is None:
        return

    unsupported: list[str] = []
    try:
        async with pool.connection(timeout=2.0) as conn:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT DISTINCT stream_mode
                  FROM guardrails.policies
                 WHERE status = 'active' AND stream_mode IS NOT NULL
                """
            )
            unsupported.extend(
                str(row[0]) for row in await cur.fetchall()
                if str(row[0]) not in SUPPORTED_STREAM_MODES
            )
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT DISTINCT default_stream_mode
                  FROM guardrails.rules
                 WHERE status = 'active' AND default_stream_mode IS NOT NULL
                """
            )
            unsupported.extend(
                str(row[0]) for row in await cur.fetchall()
                if str(row[0]) not in SUPPORTED_STREAM_MODES
            )
    except Exception as exc:  # noqa: BLE001 — DB hiccup at boot is readiness' job, not fail-fast
        logger.warning("stream_mode_check_skipped", reason="db_unavailable", error=str(exc))
        return

    if unsupported:
        modes = sorted(set(unsupported))
        logger.error("unsupported_stream_mode", modes=modes, supported=sorted(SUPPORTED_STREAM_MODES))
        raise UnsupportedStreamModeError(
            f"Active policies/rules require unsupported stream mode(s) {modes}; "
            f"this build supports {sorted(SUPPORTED_STREAM_MODES)}."
        )
