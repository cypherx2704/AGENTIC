"""mcp-eng-memory test fixtures. Network-free: require_principal is overridden and the
backend proxy is replaced with a fake, so no Auth/JWKS or cypherx-a1 backend is needed."""

from __future__ import annotations

import os
import pathlib

# Point the manifest loader at the committed source-of-truth before the app imports settings.
_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "manifest.json"
os.environ.setdefault("MANIFEST_PATH", str(_MANIFEST))

import pytest
from fastapi.testclient import TestClient

from mcp_eng_memory.core.auth import Principal, require_principal
from mcp_eng_memory.main import create_app


class FakeBackend:
    """Stand-in for the cypherx-a1 proxy: returns canned cited responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def graph(self, path: str, body: dict, *, agent_jwt: str) -> dict:
        self.calls.append((path, body))
        return {"items": [{"target": body.get("target") or body.get("topic")}],
                "citations": [{"kind": "entity", "title": "acme/payments"}]}

    async def ask(self, question: str, *, agent_jwt: str) -> dict:
        self.calls.append(("/v1/copilot/ask", {"question": question}))
        return {"answer": "because X", "citations": [{"kind": "chunk", "title": "PR 101"}]}

    async def aclose(self) -> None:  # parity with BackendClient
        pass


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id="00000000-0000-0000-0000-0000000000aa",
        agent_id="11111111-1111-1111-1111-111111111111",
        scopes=["tool:invoke", "tool:mcp-eng-memory:invoke"],
        agent_jwt="agent.jwt",
    )


@pytest.fixture
def client(principal: Principal) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as c:
        c.app.state.backend = FakeBackend()
        yield c
    app.dependency_overrides.clear()
