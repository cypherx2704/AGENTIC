"""LLMs-gateway client (chat + embeddings) — the ONLY path to a provider.

Used for (a) knowledge-extraction chat (``response_format=json_object``), (b) copilot
answer generation, and (c) corpus/query embeddings. Identity via HEADERS only (Contract 12
service JWT + forwarded agent JWT + W3C trace). ``Idempotency-Key`` is supported on chat so
a retried extraction worker replays the gateway result instead of re-spending. The gateway
owns cost metering (``usage.cost_usd`` + ``llm_call_id``); cypherx-a1 never rewrites those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ChatCompletion:
    content: str | None
    finish_reason: str | None
    model: str
    usage: Usage = field(default_factory=Usage)
    llm_call_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LlmsClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.llms_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(
        self, *, agent_jwt: str, on_behalf_of: str | None, idempotency_key: str | None = None
    ) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float = 0.7,
        response_format: dict[str, Any] | None = None,
        agent_jwt: str,
        on_behalf_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> ChatCompletion:
        """Single non-streaming round-trip. Body carries NO identity (Contract 13)."""
        headers = await self._headers(
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of, idempotency_key=idempotency_key
        )
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format is not None:
            body["response_format"] = response_format
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/chat/completions"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("llms", "error").inc()
            logger.warning("llms_call_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "LLMs gateway unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("llms", "rejected").inc()
            logger.warning("llms_call_rejected", status=resp.status_code)
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"LLMs gateway returned {resp.status_code}.")
        metrics.downstream_calls_total.labels("llms", "ok").inc()
        return self._parse_chat(resp.json())

    async def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> list[list[float]]:
        """Embeddings via ``POST /v1/embeddings``. NOTE: for the engineering CORPUS, cypherx
        -a1 embeds INDIRECTLY through RAG (single embedding-cost owner). This direct path is
        only for ad-hoc query-time embeddings if ever needed; corpus ingest uses RAG."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {"model": model, "input": inputs}
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/embeddings"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("llms", "error").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "LLMs gateway unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("llms", "rejected").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"LLMs gateway returned {resp.status_code}.")
        metrics.downstream_calls_total.labels("llms", "ok").inc()
        data = resp.json()
        return [item.get("embedding", []) for item in data.get("data", [])]

    @staticmethod
    def _parse_chat(data: dict[str, Any]) -> ChatCompletion:
        choices = data.get("choices") or [{}]
        choice = choices[0]
        message = choice.get("message") or {}
        u = data.get("usage") or {}
        return ChatCompletion(
            content=message.get("content"),
            finish_reason=choice.get("finish_reason"),
            model=data.get("model", ""),
            usage=Usage(
                prompt_tokens=int(u.get("prompt_tokens", 0)),
                completion_tokens=int(u.get("completion_tokens", 0)),
                total_tokens=int(u.get("total_tokens", 0)),
                cost_usd=float(u.get("cost_usd", 0.0)),
            ),
            llm_call_id=data.get("llm_call_id") or data.get("id"),
            raw=data,
        )
