"""Contextual ingest (Anthropic-style contextual retrieval) — llms-gateway chat with a mock.

When ``RAG_CONTEXTUAL_INGEST`` is on, each chunk is given a short (1-2 sentence) situating
context derived from the whole document. That context is:
  * prepended to the chunk text BEFORE embedding (so the dense vector carries the situating
    signal), and
  * stored in ``chunk.metadata['context']`` so the GENERATED ``content_tsv`` column folds it
    into the lexical leg too (migration 0003).

This mirrors the embeddings/rerank clients: a Contract-12 service JWT +
``X-Forwarded-Agent-JWT``, ``X-Request-ID`` / ``traceparent`` forwarding, and mock-tolerance
so keyless local dev + tests need no gateway. It is FAIL-SOFT: any error falls back to the
raw chunk (empty context) so ingest never breaks — the DEFAULT (flag off) path is unchanged.

The gateway contract is OpenAI-shaped + additionalProperties-tolerant: we POST
``/v1/chat/completions`` and read ``choices[0].message.content``.
"""

from __future__ import annotations

import hashlib

import httpx
import structlog

from ..core import trace
from ..core.config import Settings
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)

_SYSTEM = (
    "You situate a document chunk for retrieval. Given the document and one of its chunks, "
    "reply with ONE or TWO short sentences of context that disambiguate the chunk within the "
    "document (entities, section, scope). Output ONLY the context, no preamble."
)


def _mock_context(doc_text: str, chunk: str) -> str:
    """Deterministic, dependency-free situating context (no network).

    Produces a stable 1-sentence context naming a few salient document tokens so the eval
    harness + tests can observe the lexical/dense signal it adds without a live gateway.
    """
    # The first non-trivial line of the document is a decent stand-in for a title/section.
    head = ""
    for line in doc_text.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) >= 8:
            head = line
            break
    head = head[:80] or "the document"
    tag = hashlib.sha256(chunk.encode()).hexdigest()[:6]
    return f"This excerpt is from {head} (ref {tag})."


class Contextualizer:
    """Generates per-chunk situating context via the llms-gateway with a deterministic mock."""

    def __init__(
        self,
        settings: Settings,
        *,
        token_provider: ServiceTokenProvider | None = None,
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

    def _is_mock(self) -> bool:
        return self._settings.mock_embeddings

    async def contextualize(
        self,
        doc_text: str,
        chunk: str,
        *,
        model: str | None = None,
        agent_jwt: str | None = None,
        on_behalf_of: str | None = None,
    ) -> tuple[str, str]:
        """Return ``(context, source)``. ``source`` ∈ {mock, llms, fallback_raw}.

        On any failure returns ``("", "fallback_raw")`` so the caller keeps the raw chunk.
        """
        model = model or self._settings.contextual_model
        doc_prefix = doc_text[: self._settings.contextual_max_doc_chars]
        cap = self._settings.contextual_max_context_chars

        if self._is_mock():
            return _mock_context(doc_prefix, chunk)[:cap], "mock"

        try:
            ctx = await self._via_llms(
                doc_prefix, chunk, model=model, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of
            )
            return ctx[:cap], "llms"
        except Exception as exc:  # noqa: BLE001 — fail soft: ingest must never break
            logger.warning("contextualize_fallback_raw", error=str(exc))
            return "", "fallback_raw"

    async def _via_llms(
        self,
        doc_prefix: str,
        chunk: str,
        *,
        model: str,
        agent_jwt: str | None,
        on_behalf_of: str | None,
    ) -> str:
        if self._tokens is None:
            raise RuntimeError("no service-token provider configured")
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "traceparent": trace.current_traceparent(),
            "X-Request-ID": trace.request_id_var.get(),
        }
        if agent_jwt:
            headers["X-Forwarded-Agent-JWT"] = agent_jwt
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": f"<document>\n{doc_prefix}\n</document>\n\n<chunk>\n{chunk}\n</chunk>",
                },
            ],
            "max_tokens": 120,
            "temperature": 0.0,
        }
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/chat/completions"
        resp = await self._http().post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"chat completions returned {resp.status_code}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip()
