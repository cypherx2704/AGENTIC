"""Presigned upload-url + finalize idempotency/dedup + document lifecycle."""

from __future__ import annotations

import pytest

from rag_service.db import outbox

AUTH = {"Authorization": "Bearer test"}


async def _kb(client) -> dict:  # noqa: ANN001
    return (await client.post("/v1/kbs", json={"name": "up"}, headers=AUTH)).json()


@pytest.mark.asyncio
async def test_upload_url_presign_and_validation(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "manual.pdf", "size_bytes": 1024, "content_type": "application/pdf"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "upload_url" in body and "X-Amz-Signature=" in body["upload_url"]
    assert body["doc_id"]
    assert body["expires_in"] == 900


@pytest.mark.asyncio
async def test_upload_url_content_type_rejected(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "x.exe", "size_bytes": 10, "content_type": "application/octet-stream"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["reason"] == "CONTENT_TYPE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_upload_url_size_cap_rejected(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "huge.pdf", "size_bytes": 200 * 1024 * 1024, "content_type": "application/pdf"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["reason"] == "UPLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_finalize_enqueues_ingestion_requested(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    up = (await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "m.pdf", "size_bytes": 100, "content_type": "application/pdf"},
        headers=AUTH,
    )).json()
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize",
        json={"doc_id": up["doc_id"]},
        headers={**AUTH, "Idempotency-Key": "idem-1"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "pending"
    # A self-contained ingestion.requested work-order was enqueued.
    reqs = fake_db.outbox_payloads(outbox.TOPIC_INGESTION_REQUESTED)
    assert len(reqs) == 1
    payload = reqs[0]
    assert payload["doc_id"] == up["doc_id"]
    assert payload["embedding_model_resolved"] == "text-embedding-3-small"
    assert payload["embedding_dim"] == 1536
    assert payload["chunking_strategy"] and "request_id" in payload


@pytest.mark.asyncio
async def test_finalize_idempotency_replay(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    up = (await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "m.pdf", "size_bytes": 100, "content_type": "application/pdf"},
        headers=AUTH,
    )).json()
    hdr = {**AUTH, "Idempotency-Key": "dedup-key"}
    first = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize", json={"doc_id": up["doc_id"]}, headers=hdr
    )
    assert first.status_code == 202
    # A second finalize with the SAME key replays — NO second ingestion.requested row.
    second = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize", json={"doc_id": up["doc_id"]}, headers=hdr
    )
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    reqs = fake_db.outbox_payloads(outbox.TOPIC_INGESTION_REQUESTED)
    assert len(reqs) == 1  # not duplicated


@pytest.mark.asyncio
async def test_finalize_unknown_doc_404(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize",
        json={"doc_id": "00000000-0000-0000-0000-000000000abc"},
        headers=AUTH,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_url_path_traversal_filename_422(app_client, auth_as) -> None:  # noqa: ANN001
    """BUG 2: a path-traversal filename renders as the standard 422 envelope, not a 500."""
    kb = await _kb(app_client)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/upload-url",
        json={"filename": "../../etc/passwd", "size_bytes": 10, "content_type": "application/pdf"},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_finalize_non_uuid_ids_422(app_client, auth_as) -> None:  # noqa: ANN001
    """BUG 1: a non-UUID kb_id (path) or doc_id (body) is a 422, never a 500."""
    kb = await _kb(app_client)
    bad_kb = await app_client.post(
        "/v1/kbs/not-a-uuid/documents/finalize", json={"doc_id": kb["kb_id"]}, headers=AUTH
    )
    assert bad_kb.status_code == 422, bad_kb.text
    bad_doc = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize", json={"doc_id": "not-a-uuid"}, headers=AUTH
    )
    assert bad_doc.status_code == 422, bad_doc.text


@pytest.mark.asyncio
async def test_finalize_releases_idempotency_lock_on_failure(  # noqa: ANN001
    app_client, auth_as, fake_valkey
) -> None:
    """BUG 3: a retryable failure (doc-not-found) releases the in_flight slot so a retry with
    the SAME Idempotency-Key is NOT blocked with a spurious 409 for the full TTL."""
    kb = await _kb(app_client)
    missing = "00000000-0000-0000-0000-0000000000ff"
    hdr = {**AUTH, "Idempotency-Key": "retry-key"}
    first = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize", json={"doc_id": missing}, headers=hdr
    )
    assert first.status_code == 404, first.text
    # No lingering in_flight key.
    assert list(fake_valkey._store.keys()) == []
    # Retry with the same key is retryable again (404), NOT 409 IDEMPOTENCY_REQUEST_IN_FLIGHT.
    second = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents/finalize", json={"doc_id": missing}, headers=hdr
    )
    assert second.status_code == 404, second.text


@pytest.mark.asyncio
async def test_document_status_missing_kb_404_not_403(app_client, auth_as) -> None:  # noqa: ANN001
    """MINOR: GET/DELETE document under a missing KB is 404 (KB loaded before the ACL check)."""
    missing = "00000000-0000-0000-0000-000000000999"
    doc = "00000000-0000-0000-0000-000000000abc"
    got = await app_client.get(f"/v1/kbs/{missing}/documents/{doc}", headers=AUTH)
    assert got.status_code == 404, got.text
    deleted = await app_client.delete(f"/v1/kbs/{missing}/documents/{doc}", headers=AUTH)
    assert deleted.status_code == 404, deleted.text


@pytest.mark.asyncio
async def test_inline_metadata_propagated_to_chunks_and_filters(  # noqa: ANN001
    app_client, auth_as, fake_db
) -> None:
    """BUG 4: inline document metadata lands on chunk metadata so query filters match."""
    kb = await _kb(app_client)
    content = "The widget assembly guide covers torque specs in detail."
    ing = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={
            "name": "g.md", "content": content, "source_type": "markdown",
            "metadata": {"category": "manual", "lang": "en"},
        },
        headers=AUTH,
    )
    assert ing.status_code == 201, ing.text
    chunk = next(c for c in fake_db.chunks if c["kb_id"] == kb["kb_id"])
    assert chunk["metadata"]["category"] == "manual"
    assert chunk["metadata"]["lang"] == "en"
    assert chunk["metadata"]["doc_name"] == "g.md"  # system key still present
    assert chunk["metadata"]["content_sha"]  # content_sha preserved for dedup

    match = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": content, "top_k": 3, "min_score": 0.5, "filters": {"category": "manual"}},
        headers=AUTH,
    )
    assert match.status_code == 200, match.text
    assert len(match.json()["results"]) >= 1
    # content_sha must NOT leak into query results.
    assert all("content_sha" not in r["metadata"] for r in match.json()["results"])

    miss = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": content, "top_k": 3, "min_score": 0.5, "filters": {"category": "nope"}},
        headers=AUTH,
    )
    assert miss.status_code == 200
    assert len(miss.json()["results"]) == 0


@pytest.mark.asyncio
async def test_document_list_status_delete(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _kb(app_client)
    doc = (await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "d.md", "content": "alpha beta", "source_type": "markdown"},
        headers=AUTH,
    )).json()

    listed = await app_client.get(f"/v1/kbs/{kb['kb_id']}/documents", headers=AUTH)
    assert listed.status_code == 200
    assert any(d["doc_id"] == doc["doc_id"] for d in listed.json()["documents"])

    status = await app_client.get(f"/v1/kbs/{kb['kb_id']}/documents/{doc['doc_id']}", headers=AUTH)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"

    # Delete cascades chunks + queues an s3_deletions row.
    delete = await app_client.delete(
        f"/v1/kbs/{kb['kb_id']}/documents/{doc['doc_id']}", headers=AUTH
    )
    assert delete.status_code == 204
    assert all(c["doc_id"] != doc["doc_id"] for c in fake_db.chunks)
    assert any(r["doc_id"] == doc["doc_id"] for r in fake_db.s3_deletions)


@pytest.mark.asyncio
async def test_inline_ingest_dedup_on_reingest(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    """Re-ingesting identical content into the SAME doc skips re-embedding (content_sha)."""
    from rag_service.services.embeddings import mock_embed
    from rag_service.services.ingest import ingest_text
    from rag_service.services.store.pgvector import PgVectorAdapter

    settings = app_client._app.state.settings  # type: ignore[attr-defined]
    embedder = app_client._app.state.embedder  # type: ignore[attr-defined]
    pool = app_client._app.state.db_pool  # type: ignore[attr-defined]
    store = PgVectorAdapter(pool, settings)
    _ = mock_embed  # determinism comes from the mock embedder

    kwargs = {
        "text": "alpha beta gamma. delta epsilon zeta.",
        "doc_id": "doc-x", "kb_id": "kb-x", "tenant_id": "00000000-0000-0000-0000-0000000000aa",
        "embedding_model": "text-embedding-3-small", "embedding_dim": 1536,
        "chunking_strategy": "sentence", "chunk_size": 512, "chunk_overlap": 50,
        "doc_name": "d", "source_uri": None, "embedder": embedder, "store": store,
        "settings": settings,
    }
    r1 = await ingest_text(**kwargs)
    r2 = await ingest_text(**kwargs)  # identical -> all dedup'd
    assert r1.chunks_indexed >= 1
    assert r2.chunks_indexed == 0  # nothing re-inserted
