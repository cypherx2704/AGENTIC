"""Multi-query expansion (RAG-Fusion) — llms-gateway chat with a deterministic mock.

When ``RAG_MULTIQUERY_ENABLED`` is on AND a query opts in with ``multi_query=true``, the query
is rewritten into several diverse paraphrases; the handler retrieves for each variant and fuses
the ranked lists with application-level Reciprocal Rank Fusion (``services/fusion.py``). This is
a RECALL lever for vocabulary-mismatch misses (a relevant chunk phrased differently from the
user's wording), the explicit "no query expansion" gap in the service.

Mirrors ``services/decompose.py`` / ``services/contextual.py`` exactly: a Contract-12 service
JWT + ``X-Forwarded-Agent-JWT``, ``X-Request-ID`` / ``traceparent`` forwarding, and
mock-tolerance. FAIL-SOFT: any error falls back to the ORIGINAL single query — NEVER a
fabricated result — so the DEFAULT (flag off) path is byte-identical.

The returned list ALWAYS includes the original query first, then up to ``n`` generated
variants, so fusion can never do worse than single-query retrieval on the original wording.
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
    "You expand a search query for document retrieval. Given a user query, produce diverse "
    "alternative phrasings that a relevant document might use (synonyms, expansions, related "
    "terms). Output ONE query per line, no numbering, no preamble, no repetition of the input."
)

# Deterministic, dependency-free paraphrase templates for the mock path. Each is a light lexical
# transformation that changes surface vocabulary so retrieval variants differ — enough for the
# eval harness + tests to observe fusion without a live gateway. Real logic, not fabricated data.
_MOCK_TEMPLATES = (
    "what is {q}",
    "how does {q} work",
    "{q} details and requirements",
    "information about {q}",
)


def mock_expand(query: str, n: int) -> list[str]:
    """Deterministic query expansion (no network) — public for the eval harness + tests.

    Returns ``[query] + up to n``deterministic paraphrases (de-duplicated, order-preserving).
    """
    variants: list[str] = [query.strip()]
    seen = {query.strip().lower()}
    q = query.strip().rstrip("?.").strip()
    for template in _MOCK_TEMPLATES:
        if len(variants) - 1 >= n:
            break
        cand = template.format(q=q)
        key = cand.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append(cand)
    return variants


class QueryExpander:
    """Rewrites a query into diverse variants via the llms-gateway with a mock fallback."""

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

    async def expand(
        self,
        query: str,
        *,
        n: int | None = None,
        model: str | None = None,
        agent_jwt: str | None = None,
        on_behalf_of: str | None = None,
    ) -> tuple[list[str], str]:
        """Return ``(variants, source)``. ``source`` ∈ {mock, llms, fallback_single}.

        ``variants[0]`` is ALWAYS the original query. On any failure returns
        ``([query], "fallback_single")`` so the caller runs single-query retrieval.
        """
        model = model or self._settings.multiquery_model
        n = self._settings.multiquery_num_variants if n is None else n
        n = max(0, n)

        if self._is_mock():
            return mock_expand(query, n), "mock"

        try:
            extra = await self._via_llms(
                query, model=model, n=n, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of
            )
            variants = [query.strip()]
            seen = {query.strip().lower()}
            for v in extra:
                if v.lower() not in seen and len(variants) - 1 < n:
                    seen.add(v.lower())
                    variants.append(v)
            return variants, "llms"
        except Exception as exc:  # noqa: BLE001 — fail soft: never fabricate, degrade to single
            logger.warning("multiquery_fallback_single", error=str(exc))
            return [query], "fallback_single"

    async def _via_llms(
        self,
        query: str,
        *,
        model: str,
        n: int,
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
                    "content": f"Generate {n} alternative phrasings:\n<query>\n{query}\n</query>",
                },
            ],
            "max_tokens": 220,
            "temperature": 0.3,
        }
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/chat/completions"
        resp = await self._http().post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"chat completions returned {resp.status_code}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return []
        content = str((choices[0].get("message") or {}).get("content") or "")
        lines = [
            re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip()
            for ln in content.splitlines()
        ]
        return [ln for ln in lines if len(ln) >= 3]
