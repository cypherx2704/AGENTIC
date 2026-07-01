"""Shared pytest configuration.

``conftest.py`` is imported by pytest BEFORE any test module is collected, so this is
the earliest deterministic place to pin the environment. The service caches its
``Settings`` via an ``lru_cache`` on ``get_settings()``; whichever code path calls it
first wins for the whole process. Pinning ``CLASSIFIER_MODE=stub`` (no torch/detoxify)
plus a harmless ``DATABASE_URL`` and a fixed redaction key here guarantees the app-level
tests always resolve the stub classifier and never need a real Auth, Kafka, or DB.
"""

from __future__ import annotations

import os

os.environ.setdefault("CLASSIFIER_MODE", "stub")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://grd_user:localdev@localhost:5432/cypherx_platform",
)
os.environ.setdefault("REDACTION_HMAC_KEY_PLATFORM", "test-platform-redaction-key")
