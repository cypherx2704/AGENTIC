"""Outbox envelope/usage emission + S3-deletion sweeper drain."""

from __future__ import annotations

import pytest

from rag_service.core.config import Settings
from rag_service.db import outbox
from rag_service.worker.sweeper import S3DeletionSweeper

from .conftest import TEST_TENANT
from .fakes import FakeDb, FakePool


def test_envelope_is_contract5_shaped() -> None:
    env = outbox.build_envelope(
        outbox.TOPIC_USAGE_RECORDED, TEST_TENANT, "trace-1", {"k": "v"}, producer_version="0.1.0"
    )
    assert env["event_type"] == outbox.TOPIC_USAGE_RECORDED
    assert env["tenant_id"] == TEST_TENANT
    assert env["partition_key"] == TEST_TENANT
    assert env["producer_service"] == "rag-service"
    assert env["schema_version"] == "1.0.0"
    assert env["payload"] == {"k": "v"}
    assert "event_id" in env and "produced_at" in env


@pytest.mark.asyncio
async def test_emit_usage_writes_units_only_payload() -> None:
    db = FakeDb()
    pool = FakePool(db)
    await outbox.emit_usage(
        pool,
        tenant_id=TEST_TENANT,
        trace_id="trace-1",
        request_id="req-1",
        operation="rag.query",
        units={"chunks_returned": 3, "top_k": 5},
        agent_id="agent-1",
        producer_version="0.1.0",
    )
    payloads = db.outbox_payloads(outbox.TOPIC_USAGE_RECORDED)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["operation"] == "rag.query"
    assert p["units"] == {"chunks_returned": 3, "top_k": 5}
    assert p["request_id"] == "req-1"
    # Contract-14 single-owner rule: NO cost fields in the RAG usage event.
    assert "cost_usd" not in p


@pytest.mark.asyncio
async def test_s3_deletion_sweeper_drains_pending() -> None:
    db = FakeDb()
    pool = FakePool(db)
    db.s3_deletions.append({
        "doc_id": "d1", "tenant_id": TEST_TENANT, "s3_prefix": f"{TEST_TENANT}/d1/",
        "requested_at": None, "attempts": 0,
    })

    class _Store:
        async def delete_prefix(self, prefix: str) -> bool:
            return True

    sweeper = S3DeletionSweeper(pool, _Store(), Settings())
    deleted = await sweeper.sweep_once()
    assert deleted == 1
    assert db.s3_deletions == []


@pytest.mark.asyncio
async def test_s3_deletion_sweeper_retries_on_failure() -> None:
    db = FakeDb()
    pool = FakePool(db)
    db.s3_deletions.append({
        "doc_id": "d2", "tenant_id": TEST_TENANT, "s3_prefix": f"{TEST_TENANT}/d2/",
        "requested_at": None, "attempts": 0,
    })

    class _Store:
        async def delete_prefix(self, prefix: str) -> bool:
            return False  # S3 down -> leave the row + bump attempts

    sweeper = S3DeletionSweeper(pool, _Store(), Settings())
    deleted = await sweeper.sweep_once()
    assert deleted == 0
    assert db.s3_deletions[0]["attempts"] == 1
