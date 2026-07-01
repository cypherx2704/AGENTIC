"""ValkeyClient (WP02 foundation): lazy connect, fail-soft ping, valkey_up gauge."""

from __future__ import annotations

from guardrails_service.core import metrics
from guardrails_service.core.valkey import ValkeyClient


class _FakeRedis:
    def __init__(self, ok: bool) -> None:
        self._ok = ok
        self.pings = 0
        self.closed = False

    async def ping(self) -> bool:
        self.pings += 1
        if not self._ok:
            raise ConnectionError("valkey down")
        return True

    async def aclose(self) -> None:
        self.closed = True


async def test_ping_ok_sets_gauge_up() -> None:
    fake = _FakeRedis(ok=True)
    client = ValkeyClient("redis://unused", client=fake)  # type: ignore[arg-type]
    assert await client.ping() is True
    assert fake.pings == 1
    assert metrics.valkey_up._value.get() == 1


async def test_ping_failure_is_fail_soft_and_sets_gauge_down() -> None:
    client = ValkeyClient("redis://unused", client=_FakeRedis(ok=False))  # type: ignore[arg-type]
    assert await client.ping() is False  # never raises — soft dependency
    assert metrics.valkey_up._value.get() == 0


async def test_real_client_is_lazy_and_unreachable_is_fail_soft() -> None:
    # No connection is attempted at construction; the first ping against an
    # unreachable port fails SOFT (False), it does not raise.
    client = ValkeyClient("redis://127.0.0.1:1/0", timeout_seconds=0.2)
    assert await client.ping() is False
    await client.aclose()


async def test_aclose_without_ever_connecting_is_noop() -> None:
    fake = _FakeRedis(ok=True)
    client = ValkeyClient("redis://unused", client=fake)  # type: ignore[arg-type]
    await client.aclose()
    assert fake.closed is True
    await ValkeyClient("redis://unused").aclose()  # never connected -> no-op
