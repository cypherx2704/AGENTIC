"""Shared pytest configuration.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this
is the earliest deterministic place to pin the environment. The gateway caches its
``Settings`` via an ``lru_cache`` on ``get_settings()``; whichever code path calls it
first wins for the whole process. Pinning MOCK_PROVIDERS + a harmless DATABASE_URL
here guarantees the app-level tests always resolve the deterministic mock provider
and never need a real provider key, Auth, Kafka, or DB — regardless of the order in
which test modules happen to import the app.
"""

from __future__ import annotations

import os

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://llms_user:localdev@localhost:5432/cypherx_platform",
)
