"""Shared test fixtures. Network-free: the DB pool is not opened at startup and the outbox
publisher is disabled, so the app boots with no Postgres/Kafka/Valkey. Downstream clients
are lazy (no connections until used). require_principal is overridden per test."""

from __future__ import annotations

import os

# Must be set BEFORE the app imports get_settings (SERVICE_BOOTSTRAP_SECRET has no default).
os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "test-secret")
os.environ.setdefault("DB_POOL_OPEN_AT_STARTUP", "false")
os.environ.setdefault("OUTBOX_PUBLISHER_ENABLED", "false")
os.environ.setdefault("REVOCATION_CHECK_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from cypherx_a1.core.auth import Principal, require_principal
from cypherx_a1.main import create_app


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id="00000000-0000-0000-0000-0000000000aa",
        agent_id="11111111-1111-1111-1111-111111111111",
        scopes=["cypherxa1:query", "cypherxa1:ingest"],
        raw_token="agent.jwt.token",
    )


@pytest.fixture
def client(principal: Principal) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
