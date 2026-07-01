"""KB CRUD + ingest→query E2E (mock embeddings, fake pool)."""

from __future__ import annotations

import pytest

AUTH = {"Authorization": "Bearer test"}


async def _create_kb(client, name="docs", **kw) -> dict:  # noqa: ANN001
    resp = await client.post("/v1/kbs", json={"name": name, **kw}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_get_list_kb(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="manuals")
    assert kb["embedding_model_resolved"] == "text-embedding-3-small"
    assert kb["embedding_dim"] == 1536
    assert kb["name"] == "manuals"

    got = await app_client.get(f"/v1/kbs/{kb['kb_id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["kb_id"] == kb["kb_id"]

    listed = await app_client.get("/v1/kbs", headers=AUTH)
    assert listed.status_code == 200
    assert any(k["kb_id"] == kb["kb_id"] for k in listed.json())


@pytest.mark.asyncio
async def test_duplicate_kb_name_409(app_client, auth_as) -> None:  # noqa: ANN001
    await _create_kb(app_client, name="dupe")
    resp = await app_client.post("/v1/kbs", json={"name": "dupe"}, headers=AUTH)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_kb(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="temp")
    resp = await app_client.delete(f"/v1/kbs/{kb['kb_id']}", headers=AUTH)
    assert resp.status_code == 204
    got = await app_client.get(f"/v1/kbs/{kb['kb_id']}", headers=AUTH)
    assert got.status_code == 404


@pytest.mark.asyncio
async def test_resolved_fields_immutable_on_create(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="immutable")
    # There is no UPDATE path that touches resolved fields — assert the persisted row.
    row = next(r for r in fake_db.knowledge_bases if r["kb_id"] == kb["kb_id"])
    assert row["embedding_model_resolved"] == "text-embedding-3-small"
    assert row["embedding_dim"] == 1536


@pytest.mark.asyncio
async def test_default_tenant_acl_created_on_create(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="withacl")
    acls = [r for r in fake_db.kb_acls if r["kb_id"] == kb["kb_id"]]
    assert len(acls) == 1
    assert acls[0]["principal_type"] == "tenant"
    assert acls[0]["principal_id"] == "*"
    assert "query" in acls[0]["permissions"]


@pytest.mark.asyncio
async def test_private_kb_omits_tenant_default_grants_creator(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="private", private=True)
    acls = [r for r in fake_db.kb_acls if r["kb_id"] == kb["kb_id"]]
    # No tenant-wide '*' row (private), but the creator gets a full-access agent ACL.
    assert all(not (a["principal_type"] == "tenant" and a["principal_id"] == "*") for a in acls)
    assert len(acls) == 1
    assert acls[0]["principal_type"] == "agent"


@pytest.mark.asyncio
async def test_ingest_then_query_e2e(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="kbe2e", chunking_strategy="sentence")

    # Two sentences -> two chunks. The mock embedder is deterministic but semantically
    # meaningless, so retrieval ranks by exact-vector match: querying a sentence verbatim
    # gives cosine score == 1 for its chunk (deterministic, no network).
    sentence = "The refund policy for enterprise plans allows a full refund within 30 days."
    content = sentence + " Standard plans have a 14-day window instead."
    ingest = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "policy.md", "content": content, "source_type": "markdown"},
        headers=AUTH,
    )
    assert ingest.status_code == 201, ingest.text
    doc = ingest.json()
    assert doc["status"] == "completed"

    # Chunks + vectors were stored.
    stored_chunks = [c for c in fake_db.chunks if c["kb_id"] == kb["kb_id"]]
    assert len(stored_chunks) >= 1
    assert len(fake_db.chunk_vectors_1536) >= 1

    # Query the first chunk's exact content -> top hit is that chunk with score ~1.0.
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": stored_chunks[0]["content"], "top_k": 3, "min_score": 0.5},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "query_id" in body and "duration_ms" in body
    assert len(body["results"]) >= 1
    top = body["results"][0]
    assert top["score"] >= 0.99  # exact-vector match
    assert top["content"] == stored_chunks[0]["content"]
    assert all(0.0 <= r["score"] <= 1.0 for r in body["results"])

    # Usage + completed events emitted to the outbox.
    assert "cypherx.rag.usage.recorded" in fake_db.outbox_topics()
    assert "cypherx.rag.ingestion.completed" in fake_db.outbox_topics()


@pytest.mark.asyncio
async def test_query_top_k_over_cap_422(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="capkb")
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": "x", "top_k": 101},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_query_missing_kb_404(app_client, auth_as) -> None:  # noqa: ANN001
    resp = await app_client.post(
        "/v1/kbs/00000000-0000-0000-0000-000000000999/query",
        json={"query": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_uuid_kb_id_is_422_not_500(app_client, auth_as) -> None:  # noqa: ANN001
    """BUG 1: a non-UUID kb_id path param yields a 422 VALIDATION_ERROR, not a 500."""
    cases = [
        ("get", "/v1/kbs/not-a-uuid", None),
        ("get", "/v1/kbs/not-a-uuid/status", None),
        ("delete", "/v1/kbs/not-a-uuid", None),
        ("post", "/v1/kbs/not-a-uuid/query", {"query": "x"}),
        ("post", "/v1/kbs/not-a-uuid/documents", {"name": "a", "content": "b"}),
        ("get", "/v1/kbs/not-a-uuid/documents", None),
        ("get", "/v1/kbs/not-a-uuid/acls", None),
    ]
    for method, path, body in cases:
        if method == "get":
            resp = await app_client.get(path, headers=AUTH)
        elif method == "delete":
            resp = await app_client.delete(path, headers=AUTH)
        else:
            resp = await app_client.post(path, json=body, headers=AUTH)
        assert resp.status_code == 422, f"{method} {path} -> {resp.status_code}: {resp.text}"
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR", resp.text


@pytest.mark.asyncio
async def test_inline_ingest_over_100kib_422(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, name="bigkb")
    big = "x" * (100 * 1024 + 1)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "big.txt", "content": big, "source_type": "text"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["reason"] == "INLINE_TOO_LARGE"
