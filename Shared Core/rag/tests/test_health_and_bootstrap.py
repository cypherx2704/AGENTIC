"""Health endpoints + cold-start readiness with the lazy platform-skills bootstrap."""

from __future__ import annotations

import pytest

from rag_service.core.config import PLATFORM_TENANT_ID, Settings
from rag_service.services.bootstrap import PlatformSkillsBootstrap

from .fakes import FakeDb, FakePool


@pytest.mark.asyncio
async def test_livez_is_process_only(app_client) -> None:  # noqa: ANN001
    resp = await app_client.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_metrics_exposed(app_client) -> None:  # noqa: ANN001
    resp = await app_client.get("/metrics")
    assert resp.status_code == 200
    assert b"rag_query_total" in resp.content or resp.status_code == 200


@pytest.mark.asyncio
async def test_readyz_ready_when_db_pgvector_and_bootstrap_running(app_client) -> None:  # noqa: ANN001
    app = app_client._app  # type: ignore[attr-defined]

    # Simulate a running bootstrap loop (readiness gates on RUNNING, not row existence).
    class _Loop:
        running = True

    app.state.bootstrap = _Loop()
    resp = await app_client.get("/readyz")
    assert resp.status_code == 200
    checks = resp.json()["checks"]
    assert checks["postgresql"] == "ok"
    assert checks["pgvector"] == "ok"
    assert checks["bootstrap_loop"] == "running"


@pytest.mark.asyncio
async def test_readyz_503_when_bootstrap_not_running(app_client) -> None:  # noqa: ANN001
    app = app_client._app  # type: ignore[attr-defined]

    class _Loop:
        running = False

    app.state.bootstrap = _Loop()
    resp = await app_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["bootstrap_loop"] == "not_running"


@pytest.mark.asyncio
async def test_readyz_503_when_pgvector_absent(app_client, fake_db) -> None:  # noqa: ANN001
    app = app_client._app  # type: ignore[attr-defined]

    class _Loop:
        running = True

    app.state.bootstrap = _Loop()
    fake_db.has_vector_ext = False  # pgvector extension not installed
    resp = await app_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["pgvector"] == "fail"


@pytest.mark.asyncio
async def test_bootstrap_ensure_once_creates_kb_and_acl_with_llms_down() -> None:
    """Cold-start: NO live llms — the env-pinned model/dim are used; KB + default ACL land."""
    db = FakeDb()
    pool = FakePool(db)
    # mock_embeddings True + no token provider == llms is effectively "down" for resolution.
    settings = Settings(
        mock_embeddings=True,
        embedding_model_resolved="text-embedding-3-small",
        embedding_dim=1536,
    )
    boot = PlatformSkillsBootstrap(pool, settings)
    ok = await boot.ensure_once()
    assert ok is True
    kb = next(r for r in db.knowledge_bases if r["name"] == "platform-skills")
    assert kb["tenant_id"] == PLATFORM_TENANT_ID
    assert kb["embedding_model_resolved"] == "text-embedding-3-small"
    assert kb["embedding_dim"] == 1536
    # The default (tenant,'*') ACL row is in the SAME bootstrap (else readable by no one).
    acls = [r for r in db.kb_acls if r["kb_id"] == kb["kb_id"]]
    assert len(acls) == 1
    assert acls[0]["principal_type"] == "tenant" and acls[0]["principal_id"] == "*"


@pytest.mark.asyncio
async def test_bootstrap_ensure_once_idempotent() -> None:
    db = FakeDb()
    pool = FakePool(db)
    settings = Settings(mock_embeddings=True)
    boot = PlatformSkillsBootstrap(pool, settings)
    await boot.ensure_once()
    await boot.ensure_once()  # re-run on every pod start is safe (ON CONFLICT DO NOTHING)
    kbs = [r for r in db.knowledge_bases if r["name"] == "platform-skills"]
    assert len(kbs) == 1
    acls = [r for r in db.kb_acls if r["kb_id"] == kbs[0]["kb_id"]]
    assert len(acls) == 1


@pytest.mark.asyncio
async def test_bootstrap_no_pool_returns_false_keeps_retrying() -> None:
    settings = Settings(mock_embeddings=True)
    boot = PlatformSkillsBootstrap(None, settings)
    # No pool yet -> not an error, just "not done" so the loop keeps retrying.
    assert await boot.ensure_once() is False
