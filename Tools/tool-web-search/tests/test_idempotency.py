"""POST /mcp/v1/invoke — Idempotency-Key replay (Valkey-backed, fail-open)."""

from __future__ import annotations

import pytest

from .conftest import DownValkey, FakeValkey

_INVOKE = "/mcp/v1/invoke"


@pytest.mark.asyncio
async def test_repeated_key_replays_same_result(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    ac = await make_client(valkey=valkey)
    headers = {"Idempotency-Key": "abc-123"}

    first = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers.get("Idempotency-Replayed") is None

    # Same key -> replay the stored body verbatim, flagged as a replay.
    second = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first.json()


@pytest.mark.asyncio
async def test_different_key_does_not_replay(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    ac = await make_client(valkey=valkey)

    a = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers={"Idempotency-Key": "k1"})
    b = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers={"Idempotency-Key": "k2"})
    assert a.status_code == b.status_code == 200
    assert b.headers.get("Idempotency-Replayed") is None


@pytest.mark.asyncio
async def test_no_key_never_replays(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    ac = await make_client(valkey=valkey)
    r1 = await ac.post(_INVOKE, json={"args": {"query": "idem"}})
    r2 = await ac.post(_INVOKE, json={"args": {"query": "idem"}})
    assert r1.status_code == r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") is None


@pytest.mark.asyncio
async def test_valkey_down_fail_open_no_replay(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client(valkey=DownValkey())
    headers = {"Idempotency-Key": "abc-down"}
    r1 = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers=headers)
    r2 = await ac.post(_INVOKE, json={"args": {"query": "idem"}}, headers=headers)
    # Both proceed (fail-open) and neither is a replay.
    assert r1.status_code == r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") is None
