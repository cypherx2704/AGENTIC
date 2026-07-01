"""POST /v1/rerank — cross-encoder reranking surface (pluggable provider, mock default).

Deterministic, no live infra. Drives the ASGI app with ``mock_providers=true`` and the
standard ``require_principal`` override (no real Auth / JWKS / Kafka). The default
``RERANK_PROVIDER=mock`` returns deterministic lexical-overlap scores so order / shape /
usage can be asserted offline. ``db_pool=None`` makes the usage-write path a no-op.

Covers:
* string query + documents -> 200 with ``results[]`` ({index,score}) + ``model`` + ``usage``.
* relevance ordering is deterministic + stable (the most-overlapping doc ranks first).
* ``id`` is echoed back on the matching result when the request document carried one.
* ``top_n`` truncates to the top N by score.
* > rerank_max_documents -> 413 ``DOCUMENTS_EXCEEDED`` (before the provider).
* over the payload-byte cap -> 413 ``PAYLOAD_BYTES_EXCEEDED``.
* empty documents / empty query -> 422 ``VALIDATION_ERROR`` (pydantic).
* ``RERANK_PROVIDER=local`` -> 503 ``RERANK_LOCAL_UNAVAILABLE`` (seam, not default image).
* Idempotency-Key replay -> same body + ``Idempotency-Replayed: true``; in-flight -> 409;
  no Valkey -> fail-open (proceeds, no replay).
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke"],
        principal_type="agent",
    )


class FakeValkey:
    """In-memory ValkeyClient stand-in (same surface idempotency uses)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None,
                  timeout_seconds: float | None = None) -> None:
        self.store[key] = value

    async def set_if_absent(self, key: str, value: str, *, ttl_seconds: int,
                            timeout_seconds: float | None = None) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def incr_with_expire(self, key: str, *, ttl_seconds: int,
                               timeout_seconds: float | None = None) -> int:
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n

    async def incrby_with_expire(self, key: str, amount: int, *, ttl_seconds: int,
                                 timeout_seconds: float | None = None) -> int:
        n = int(self.store.get(key, "0")) + amount
        self.store[key] = str(n)
        return n


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None  # usage-write no-ops
        app.state.valkey = None  # per-test override
        app.state.settings = Settings()  # fresh per-test (tests mutate caps live)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


_DOCS = [
    {"id": "d0", "text": "the quick brown fox jumps over the lazy dog"},
    {"id": "d1", "text": "a treatise on quantum chromodynamics"},
    {"id": "d2", "text": "brown bears and brown foxes roam the forest"},
]


@pytest.mark.asyncio
async def test_rerank_shape(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post(
        "/v1/rerank",
        json={"model": "rerank-default", "query": "brown fox", "documents": _DOCS},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"]  # echoes the resolved model id
    assert isinstance(body["results"], list) and len(body["results"]) == 3
    for r in body["results"]:
        assert isinstance(r["index"], int) and r["index"] >= 0
        assert isinstance(r["score"], (int, float))
    usage = body["usage"]
    assert usage["total_tokens"] > 0
    assert usage["search_units"] == 3  # one billable unit per candidate document


@pytest.mark.asyncio
async def test_rerank_deterministic_ordering_and_id_echo(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    payload = {"model": "rerank-default", "query": "brown fox", "documents": _DOCS}
    first = await ac.post("/v1/rerank", json=payload)
    second = await ac.post("/v1/rerank", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    # Fully deterministic: byte-for-byte identical between calls.
    assert first.json() == second.json()
    results = first.json()["results"]
    # "the quick brown fox" (d0) has both query terms -> ranks above the QCD doc (d1).
    top = results[0]
    assert top["index"] == 0
    assert top["id"] == "d0"  # caller-supplied id echoed
    indices = [r["index"] for r in results]
    assert indices[-1] == 1  # the unrelated QCD doc ranks last (score 0)


@pytest.mark.asyncio
async def test_rerank_top_n_truncates(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post(
        "/v1/rerank",
        json={"model": "rerank-default", "query": "brown fox", "documents": _DOCS, "top_n": 2},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 2
    # Truncation keeps the top-scored entries.
    assert results[0]["score"] >= results[1]["score"]


@pytest.mark.asyncio
async def test_rerank_too_many_documents_413(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    over = [{"text": "t"}] * (app.state.settings.rerank_max_documents + 1)
    resp = await ac.post("/v1/rerank", json={"model": "rerank-default", "query": "q", "documents": over})
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "DOCUMENTS_EXCEEDED"
    assert err["details"]["documents"] == len(over)


@pytest.mark.asyncio
async def test_rerank_payload_bytes_413(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.settings.rerank_max_payload_bytes = 8
    resp = await ac.post(
        "/v1/rerank",
        json={"model": "rerank-default", "query": "this is more than eight bytes",
              "documents": [{"text": "also long enough"}]},
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "PAYLOAD_BYTES_EXCEEDED"
    assert err["details"]["max_bytes"] == 8


@pytest.mark.asyncio
async def test_rerank_empty_documents_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post("/v1/rerank", json={"model": "rerank-default", "query": "q", "documents": []})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_rerank_empty_query_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post(
        "/v1/rerank", json={"model": "rerank-default", "query": "", "documents": [{"text": "x"}]}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_rerank_local_provider_seam_503(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Flip the provider flag live: the local cross-encoder seam is not in the default image.
    app.state.settings.rerank_provider = "local"
    resp = await ac.post(
        "/v1/rerank", json={"model": "rerank-default", "query": "q", "documents": [{"text": "x"}]}
    )
    assert resp.status_code == 503, resp.text
    err = resp.json()["error"]
    assert err["code"] == "SERVICE_UNAVAILABLE"
    assert err["details"]["reason"] == "RERANK_LOCAL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_rerank_idempotency_replay(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.valkey = FakeValkey()
    payload = {"model": "rerank-default", "query": "brown fox", "documents": _DOCS}

    first = await ac.post("/v1/rerank", headers={"Idempotency-Key": "r-1"}, json=payload)
    assert first.status_code == 200, first.text
    assert first.headers.get("Idempotency-Replayed") is None
    first_body = first.json()

    stored = json.loads(next(v for k, v in app.state.valkey.store.items() if k.endswith(":r-1")))
    assert stored["state"] == "completed"

    second = await ac.post("/v1/rerank", headers={"Idempotency-Key": "r-1"}, json=payload)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first_body


@pytest.mark.asyncio
async def test_rerank_idempotency_in_flight_409(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    valkey = FakeValkey()
    app.state.valkey = valkey
    from llms_gateway.core.config import get_settings

    s = get_settings()
    rk = f"{s.idempotency_key_prefix}{TEST_TENANT}:r-busy"
    valkey.store[rk] = json.dumps({"state": "in_flight", "stream": False})

    resp = await ac.post(
        "/v1/rerank", headers={"Idempotency-Key": "r-busy"},
        json={"model": "rerank-default", "query": "q", "documents": [{"text": "x"}]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_IN_FLIGHT"


@pytest.mark.asyncio
async def test_rerank_no_valkey_fail_open(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.valkey = None
    payload = {"model": "rerank-default", "query": "q", "documents": [{"text": "x"}]}
    first = await ac.post("/v1/rerank", headers={"Idempotency-Key": "r-x"}, json=payload)
    second = await ac.post("/v1/rerank", headers={"Idempotency-Key": "r-x"}, json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") is None
