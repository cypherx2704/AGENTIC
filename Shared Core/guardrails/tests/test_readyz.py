"""/readyz gating (WP02): rules-registry mismatch FAILS readiness; Valkey is SOFT.

State is injected directly onto ``app.state`` (no lifespan), with tiny fakes for the
hard deps: a pool whose ``SELECT 1`` succeeds, ``PolicyEngine(None)`` (built-in
platform default stands in), and the stub classifier. The registry seam consumed by
the endpoint is just ``.status``; Valkey is a real :class:`ValkeyClient` around a fake
redis so the gauge path is exercised too.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from guardrails_service.core import metrics
from guardrails_service.core.valkey import ValkeyClient
from guardrails_service.main import create_app
from guardrails_service.services.classifier import StubClassifier
from guardrails_service.services.policy_engine import PolicyEngine


class _ReadyConn:
    async def execute(self, query: str, params: object = None) -> None:
        return None


class _ReadyConnCtx:
    async def __aenter__(self) -> _ReadyConn:
        return _ReadyConn()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _ReadyPool:
    """A pool whose readiness ping (SELECT 1) always succeeds."""

    def connection(self, timeout: float | None = None) -> _ReadyConnCtx:
        return _ReadyConnCtx()


class _StaticRegistry:
    """Stands in for RuleRegistryOverlay — the endpoint consumes only ``.status``."""

    def __init__(self, status: str) -> None:
        self.status = status


class _FakeRedis:
    def __init__(self, ok: bool) -> None:
        self._ok = ok

    async def ping(self) -> bool:
        if not self._ok:
            raise ConnectionError("valkey down")
        return True

    async def aclose(self) -> None:
        return None


def _app(*, registry_status: str, valkey_ok: bool):  # noqa: ANN202 — test helper
    app = create_app()
    app.state.db_pool = _ReadyPool()
    app.state.policy_engine = PolicyEngine(None)
    app.state.classifier = StubClassifier()
    app.state.rule_registry = _StaticRegistry(registry_status)
    app.state.valkey = ValkeyClient("redis://unused", client=_FakeRedis(valkey_ok))  # type: ignore[arg-type]
    return app


async def _readyz(app) -> tuple[int, dict]:  # noqa: ANN001 — test helper
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/readyz")
    return resp.status_code, resp.json()


async def test_readyz_ok_and_valkey_down_is_soft() -> None:
    status, body = await _readyz(_app(registry_status="ok", valkey_ok=False))
    assert status == 200
    assert body["ready"] is True
    assert body["checks"]["rules_registry"] == "ok"
    # Valkey is reported but NEVER gates readiness; the gauge reflects the last ping.
    assert body["checks"]["valkey"] == "unavailable"
    assert metrics.valkey_up._value.get() == 0


async def test_readyz_fails_on_rules_registry_mismatch() -> None:
    status, body = await _readyz(_app(registry_status="mismatch", valkey_ok=True))
    assert status == 503
    assert body["ready"] is False
    assert body["checks"]["rules_registry"] == "mismatch"
    # The hard deps themselves were fine — the mismatch alone failed readiness.
    assert body["checks"]["postgresql"] == "ok"
    assert body["checks"]["classifier"] == "ok"


async def test_readyz_registry_unavailable_is_soft() -> None:
    status, body = await _readyz(_app(registry_status="unavailable", valkey_ok=True))
    assert status == 200
    assert body["checks"]["rules_registry"] == "unavailable"
    assert body["checks"]["valkey"] == "ok"
    assert metrics.valkey_up._value.get() == 1
