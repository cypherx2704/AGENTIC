"""Backend proxy client — calls the cypherx-a1 product API.

Forwards the resolved agent JWT (Authorization: Bearer) + W3C trace headers to the
cypherx-a1 ``/v1/graph/*`` and ``/v1/copilot/ask`` endpoints. cypherx-a1 re-verifies the
agent JWT and enforces tenant RLS, so this facade carries no tenant logic of its own.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..core import trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)


class BackendClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.backend_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any], *, agent_jwt: str) -> dict[str, Any]:
        url = f"{self._settings.cypherxa1_base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {agent_jwt}", **trace.propagation_headers()}
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            logger.warning("backend_call_failed", path=path, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Engineering-memory backend unavailable.") from exc
        if resp.status_code in (401, 403):
            raise ApiError(ErrorCode.FORBIDDEN, "Backend rejected the forwarded agent token.")
        if resp.status_code >= 400:
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"Backend returned {resp.status_code}.")
        return resp.json()

    async def graph(self, path: str, body: dict[str, Any], *, agent_jwt: str) -> dict[str, Any]:
        return await self._post(path, body, agent_jwt=agent_jwt)

    async def ask(self, question: str, *, agent_jwt: str) -> dict[str, Any]:
        return await self._post("/v1/copilot/ask", {"question": question}, agent_jwt=agent_jwt)
