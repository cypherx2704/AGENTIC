"""WP06 — POST /v1/embeddings (RAG + Memory dependency).

Deterministic, no live infra. Drives the ASGI app with ``mock_providers=true`` and the
standard ``require_principal`` override (no real Auth / JWKS / Kafka). The mock provider
returns a deterministic pseudo-embedding so shape / dimensions / usage can be asserted
without a real provider. ``db_pool=None`` makes the usage-write path a no-op.

Covers (per the WP06 contract):
* string ``input`` -> 200 with ``data[]`` of ``{embedding, index}`` + ``usage``.
* list ``input`` -> 200 with one ``data`` entry per item, native dimension by default.
* ``dimensions`` honoured -> the returned vector length equals it.
* >256 items -> 413 ``INPUT_ITEMS_EXCEEDED`` (the item-count cap, before the provider).
* over the payload-byte cap -> 413 ``PAYLOAD_BYTES_EXCEEDED``.
* empty input (``""`` / ``[]``) -> 422 ``VALIDATION_ERROR`` (pydantic min_length).
* Idempotency-Key replay -> same body + ``Idempotency-Replayed: true`` (fake Valkey);
  an in-flight twin -> 409; no Valkey -> fail-open (proceeds, no replay).
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
        # Fresh, per-test settings: tests mutate caps live, and the shared
        # get_settings() lru_cache singleton would otherwise leak across tests.
        app.state.settings = Settings()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


@pytest.mark.asyncio
async def test_embeddings_string_input_shape(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": "hello world"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert body["model"]  # echoes the resolved model id
    assert len(body["data"]) == 1
    entry = body["data"][0]
    assert entry["object"] == "embedding"
    assert entry["index"] == 0
    assert isinstance(entry["embedding"], list) and entry["embedding"]
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"]  # embeddings: no completion
    assert usage["cost_usd"] > 0


@pytest.mark.asyncio
async def test_embeddings_list_input_one_entry_per_item(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b", "c"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [d["index"] for d in data] == [0, 1, 2]
    # Native dimension when `dimensions` omitted (mock = text-embedding-3-small = 1536).
    assert all(len(d["embedding"]) == 1536 for d in data)


@pytest.mark.asyncio
async def test_embeddings_dimensions_honoured(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post(
        "/v1/embeddings", json={"model": "embed", "input": ["x", "y"], "dimensions": 16}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert all(len(d["embedding"]) == 16 for d in data)


@pytest.mark.asyncio
async def test_embeddings_too_many_items_413(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Cap is embeddings_max_input_items (default 256); send 257.
    over = ["t"] * (app.state.settings.embeddings_max_input_items + 1)
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": over})
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "INPUT_ITEMS_EXCEEDED"
    assert err["details"]["items"] == len(over)


@pytest.mark.asyncio
async def test_embeddings_payload_bytes_413(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Flip the byte cap low (live read on app.state.settings) to avoid sending MiB.
    app.state.settings.embeddings_max_payload_bytes = 8
    resp = await ac.post(
        "/v1/embeddings", json={"model": "embed", "input": "this is more than eight bytes"}
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "PAYLOAD_BYTES_EXCEEDED"
    assert err["details"]["max_bytes"] == 8


@pytest.mark.asyncio
async def test_embeddings_empty_string_input_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": ""})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_embeddings_empty_list_input_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post("/v1/embeddings", json={"model": "embed", "input": []})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_embeddings_idempotency_replay(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.valkey = FakeValkey()
    payload = {"model": "embed", "input": "idem please"}

    first = await ac.post("/v1/embeddings", headers={"Idempotency-Key": "e-1"}, json=payload)
    assert first.status_code == 200, first.text
    assert first.headers.get("Idempotency-Replayed") is None
    first_body = first.json()

    # A completed record exists for tenant+key.
    stored = json.loads(next(v for k, v in app.state.valkey.store.items() if k.endswith(":e-1")))
    assert stored["state"] == "completed"

    second = await ac.post("/v1/embeddings", headers={"Idempotency-Key": "e-1"}, json=payload)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first_body  # byte-for-byte replay


@pytest.mark.asyncio
async def test_embeddings_idempotency_in_flight_409(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    valkey = FakeValkey()
    app.state.valkey = valkey
    from llms_gateway.core.config import get_settings

    s = get_settings()
    rk = f"{s.idempotency_key_prefix}{TEST_TENANT}:e-busy"
    valkey.store[rk] = json.dumps({"state": "in_flight", "stream": False})

    resp = await ac.post(
        "/v1/embeddings", headers={"Idempotency-Key": "e-busy"},
        json={"model": "embed", "input": "x"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_IN_FLIGHT"


@pytest.mark.asyncio
async def test_embeddings_no_valkey_fail_open(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.valkey = None  # fail-open: no replay, no 409
    payload = {"model": "embed", "input": "no valkey"}
    first = await ac.post("/v1/embeddings", headers={"Idempotency-Key": "e-x"}, json=payload)
    second = await ac.post("/v1/embeddings", headers={"Idempotency-Key": "e-x"}, json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") is None
