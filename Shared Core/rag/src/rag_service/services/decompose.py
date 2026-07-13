"""Query decomposition (multi-hop retrieval) — llms-gateway chat with a deterministic mock.

When ``RAG_DECOMPOSE_ENABLED`` is on AND a query opts in with ``decompose=true``, a compound /
multi-hop question is split into ≤ ``decompose_max_subquestions`` focused sub-questions. The
query handler then issues one retrieval per sub-question and unions the pools, so facts
scattered across separate chunks/documents (the dominant multi-hop failure mode that a single
query vector cannot co-retrieve) are each retrieved by a focused query.

This mirrors ``services/contextual.py`` / ``services/rerank.py`` exactly: a Contract-12 service
JWT + ``X-Forwarded-Agent-JWT``, ``X-Request-ID`` / ``traceparent`` forwarding, and
mock-tolerance so keyless local dev + tests need no gateway. It is FAIL-SOFT: any error (or a
non-decomposable query) falls back to the ORIGINAL single query — NEVER a fabricated result —
so the DEFAULT (flag off) path is byte-identical and an outage degrades to today's behaviour.

The gateway contract is OpenAI-shaped + additionalProperties-tolerant: we POST
``/v1/chat/completions`` and read one sub-question per line from ``choices[0].message.content``.
"""

from __future__ import annotations

import re

import httpx
import structlog

from ..core import trace
from ..core.config import Settings
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)

_SYSTEM = (
    "You decompose a complex user question into a small set of simpler, self-contained "
    "sub-questions that can each be answered by an independent document lookup. Output ONE "
    "sub-question per line, no numbering, no preamble. If the question is already atomic, "
    "output it unchanged on a single line."
)

# Clause boundaries that typically separate independent asks in a compound question. Splitting
# on these is a real, dependency-free heuristic (not a stub). The trailing lookahead only splits
# a comma when the next clause opens with an interrogative/aux verb (a new sub-question).
_CLAUSE_OPENERS = r"what|who|when|where|why|how|which|does|do|is|are|can"
_SPLIT_RE = re.compile(
    r"\s+and\s+|\s*;\s*|\s*\?\s*|[\r\n]+|\s*,\s*(?=(?:" + _CLAUSE_OPENERS + r")\b)",
    re.IGNORECASE,
)


def mock_decompose(query: str, max_subquestions: int) -> list[str]:
    """Deterministic, dependency-free decomposition (no network) — public for the eval harness.

    Splits a compound query on clause boundaries (``and`` / ``;`` / ``?`` / newlines) into
    focused sub-questions, dropping trivially-short fragments and de-duplicating while
    preserving order. Returns ``[query]`` (a single sub-question) when the query does not
    decompose, so the caller degrades to single-query retrieval.
    """
    parts = [p.strip() for p in _SPLIT_RE.split(query)]
    subs: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if len(part) < 4:  # drop noise fragments ("a", "the", stray tokens)
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        subs.append(part)
        if len(subs) >= max_subquestions:
            break
    return subs or [query.strip()]


class QueryDecomposer:
    """Splits a compound query into sub-questions via the llms-gateway with a mock fallback."""

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

    async def decompose(
        self,
        query: str,
        *,
        model: str | None = None,
        agent_jwt: str | None = None,
        on_behalf_of: str | None = None,
    ) -> tuple[list[str], str]:
        """Return ``(sub_questions, source)``. ``source`` ∈ {mock, llms, fallback_single}.

        Always returns at least one sub-question. On any failure returns
        ``([query], "fallback_single")`` so the caller runs the un-decomposed single query.
        """
        model = model or self._settings.decompose_model
        max_n = max(1, self._settings.decompose_max_subquestions)

        if self._is_mock():
            return mock_decompose(query, max_n), "mock"

        try:
            subs = await self._via_llms(
                query, model=model, max_n=max_n, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of
            )
            return (subs[:max_n] or [query]), "llms"
        except Exception as exc:  # noqa: BLE001 — fail soft: never fabricate, degrade to single
            logger.warning("decompose_fallback_single", error=str(exc))
            return [query], "fallback_single"

    async def _via_llms(
        self,
        query: str,
        *,
        model: str,
        max_n: int,
        agent_jwt: str | None,
        on_behalf_of: str | None,
    ) -> list[str]:
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
                    "content": (
                        f"Decompose into at most {max_n} sub-questions:\n<question>\n{query}\n</question>"
                    ),
                },
            ],
            "max_tokens": 220,
            "temperature": 0.0,
        }
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/chat/completions"
        resp = await self._http().post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"chat completions returned {resp.status_code}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return [query]
        content = str((choices[0].get("message") or {}).get("content") or "")
        lines = [
            re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip()  # strip bullet/number prefixes
            for ln in content.splitlines()
        ]
        subs = [ln for ln in lines if len(ln) >= 4]
        return subs or [query]
