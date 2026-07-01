"""WP02 amended fix #3 — gateway-minted ``llm_call_id`` billing key.

Three layers, mirroring the existing test patterns (dependency overrides + fakes,
plus one real-Postgres integration test that skips when the local DB / migration
is absent):

* ``record_usage`` SQL shape: the hot-path INSERT carries ``llm_call_id``, has NO
  ``ON CONFLICT`` (fail loudly), and BOTH outbox envelopes carry ``llm_call_id``
  AND ``request_id``.
* App-level: two completions under ONE forwarded ``X-Request-ID`` produce two
  usage writes sharing the request_id but with DISTINCT freshly-minted call ids —
  both bill.
* DB-level (integration, skipped without the local test Postgres): the same two
  writes both insert; a duplicate ``(tenant_id, llm_call_id)`` raises a unique
  violation instead of being silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import uuid

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

# Force mock providers + a harmless DB URL before importing the app.
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

if sys.platform == "win32":  # psycopg async needs the selector loop (matches main.py)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.db.outbox import UsageWrite, record_usage  # noqa: E402
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


def _write(llm_call_id: str, request_id: str, tenant_id: str = TEST_TENANT) -> UsageWrite:
    return UsageWrite(
        llm_call_id=llm_call_id,
        request_id=request_id,
        tenant_id=tenant_id,
        trace_id=str(uuid.uuid4()),
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd=0.001,
        duration_ms=42,
        agent_id=TEST_AGENT,
        principal_type="agent",
    )


# ── fakes for the record_usage SQL-shape test ───────────────────────────────────────
class _RecordingConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> object:
        self.executed.append((sql, params))
        return self

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self


class _RecordingPool:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield self._conn


@pytest.mark.asyncio
async def test_hot_path_insert_carries_llm_call_id_and_no_on_conflict() -> None:
    conn = _RecordingConn()
    call_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    await record_usage(_RecordingPool(conn), _write(call_id, request_id), producer_version="0.1.0")  # type: ignore[arg-type]

    inserts = [(sql, p) for sql, p in conn.executed if "INSERT INTO llms.usage_records" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "llm_call_id" in sql
    # Amended: the hot path FAILS LOUDLY on duplicates — no silent billing drop.
    assert "ON CONFLICT" not in sql.upper()
    assert params is not None
    assert params[0] == call_id  # llm_call_id leads the column list
    assert params[1] == request_id


@pytest.mark.asyncio
async def test_both_outbox_envelopes_carry_llm_call_id_and_request_id() -> None:
    conn = _RecordingConn()
    call_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    await record_usage(_RecordingPool(conn), _write(call_id, request_id), producer_version="0.1.0")  # type: ignore[arg-type]

    outbox_rows = [p for sql, p in conn.executed if "INSERT INTO llms.outbox" in sql]
    assert len(outbox_rows) == 2  # request.completed + usage.recorded (Contract 19)
    topics = set()
    for topic, _tenant, jsonb in outbox_rows:
        topics.add(topic)
        payload = jsonb.obj["payload"]  # Jsonb wraps the Contract 5 envelope
        assert payload["llm_call_id"] == call_id
        assert payload["request_id"] == request_id
    assert topics == {"cypherx.llms.request.completed", "cypherx.llms.usage.recorded"}


# ── app-level: two calls under ONE X-Request-ID both bill ───────────────────────────
@pytest_asyncio.fixture
async def client_with_capture(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    captured: list[UsageWrite] = []

    async def _capture(pool, write, *, producer_version):  # type: ignore[no-untyped-def]
        captured.append(write)

    from llms_gateway.api import chat as chat_module

    monkeypatch.setattr(chat_module, "record_usage", _capture)

    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = object()  # non-None so the usage-write path proceeds
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, captured


@pytest.mark.asyncio
async def test_two_calls_one_request_id_mint_distinct_llm_call_ids(client_with_capture) -> None:  # type: ignore[no-untyped-def]
    ac, captured = client_with_capture
    forwarded_request_id = str(uuid.uuid4())

    for _ in range(2):
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": forwarded_request_id},
            json={"model": "smart", "messages": [{"role": "user", "content": "bill me twice"}]},
        )
        assert resp.status_code == 200, resp.text

    assert len(captured) == 2  # BOTH calls produced a billing write
    assert {w.request_id for w in captured} == {forwarded_request_id}  # correlation only
    call_ids = [w.llm_call_id for w in captured]
    assert len(set(call_ids)) == 2, "each provider call must mint a FRESH llm_call_id"
    for cid in call_ids:
        assert uuid.UUID(cid).version == 4


# ── DB-level integration (skips without the local test Postgres / migration) ────────
async def _open_pool_or_skip():  # type: ignore[no-untyped-def]
    from llms_gateway.core.config import get_settings
    from llms_gateway.db import pool as db_pool

    pool = db_pool.create_pool(get_settings().database_url)
    try:
        await pool.open(wait=True, timeout=3.0)
        async with pool.connection(timeout=3.0) as conn:
            cur = await conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='llms' AND table_name='usage_records' AND column_name='llm_call_id'"
            )
            if await cur.fetchone() is None:
                pytest.skip("WP02 migration not applied to the local test Postgres")
    except Exception as exc:  # noqa: BLE001 — no local DB -> integration test skips
        with contextlib.suppress(Exception):
            await pool.close()
        pytest.skip(f"local test Postgres unavailable: {exc}")
    return pool


@pytest.mark.asyncio
async def test_db_two_calls_one_request_id_both_bill_and_duplicates_fail_loudly() -> None:
    import psycopg

    from llms_gateway.db.pool import in_tenant

    pool = await _open_pool_or_skip()
    tenant_id = str(uuid.uuid4())  # fresh tenant per run; RLS scopes all reads/writes
    request_id = str(uuid.uuid4())
    try:
        first = _write(str(uuid.uuid4()), request_id, tenant_id)
        second = _write(str(uuid.uuid4()), request_id, tenant_id)
        await record_usage(pool, first, producer_version="test")
        await record_usage(pool, second, producer_version="test")

        async def _count(conn) -> int:  # type: ignore[no-untyped-def]
            cur = await conn.execute(
                "SELECT count(*) FROM llms.usage_records WHERE request_id = %s", (request_id,)
            )
            return (await cur.fetchone())[0]

        assert await in_tenant(pool, tenant_id, _count) == 2  # both calls billed

        # Re-using an llm_call_id is a BUG: the hot path must fail loudly.
        with pytest.raises(psycopg.errors.UniqueViolation):
            await record_usage(pool, first, producer_version="test")
    finally:
        await pool.close()
