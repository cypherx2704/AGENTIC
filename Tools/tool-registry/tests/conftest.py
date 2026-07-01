"""Shared pytest configuration.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is
the earliest deterministic place to pin the environment. The service caches its
``Settings`` via an ``lru_cache`` on ``get_settings()``; whichever code path calls it
first wins for the whole process. Pinning a harmless DATABASE_URL here guarantees the
app-level tests never need a real Auth, Valkey, or DB — the app degrades gracefully
when ``app.state.db_pool`` is ``None`` and tests inject fakes for the rest.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://tool_user:localdev@localhost:5432/cypherx_platform",
)
# Keep the platform seed out of the lifespan in app-level tests (no live DB).
os.environ.setdefault("SEED_PLATFORM_TOOLS", "false")
