"""Model-alias resolution + provider selection (Components 2 & 3).

Resolution order (Component 2): tenant-specific alias -> platform default alias ->
literal model id. The result is a ``(provider, model_id)`` pair. The provider impl is
then selected from the registered adaptors. When ``settings.mock_providers`` is true
the mock provider is always used (no keys / no network).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from ..core.config import Settings

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool
from ..core import metrics
from ..core.errors import ApiError, ErrorCode
from . import byok
from .capabilities import capability_registry
from .providers.anthropic_provider import AnthropicProvider
from .providers.base import ProviderAdaptor
from .providers.mock import MockProvider
from .providers.openai_provider import OpenAIProvider

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Resolution:
    provider: str
    model_id: str


# Cold-start fallback ONLY — mirrors the seed migration exactly (a test parses the
# seed SQL and asserts equality so the two can never drift). The DB is the single
# authority: llms.model_aliases is loaded at startup + refreshed every 60 s
# (Amendment Log 2026-06); this map is consulted only when the DB has no row.
_PLATFORM_ALIASES: dict[str, Resolution] = {
    "fast": Resolution("anthropic", "claude-haiku-4-5"),
    "smart": Resolution("anthropic", "claude-sonnet-4-6"),
    "code": Resolution("anthropic", "claude-sonnet-4-6"),
    "vision": Resolution("anthropic", "claude-sonnet-4-6"),
    "default": Resolution("anthropic", "claude-sonnet-4-6"),
    # WP06 embeddings platform default.
    "embed": Resolution("openai", "text-embedding-3-small"),
    # Rerank + safety-classify platform defaults (in-house cypherx mock/stub class).
    "rerank-default": Resolution("cypherx", "rerank-mock-v1"),
    "safety-default": Resolution("cypherx", "classify-stub-v1"),
    # Small (≈8B) open-model default — OpenAI-compatible, emulated tool-calling.
    "small": Resolution("openai", "llama-3.1-8b-instruct"),
}

# Cold-start fallback ONLY — literal model -> provider, mirroring the
# llms.model_capabilities seed (drift-guarded by the same test). At runtime the
# DB-loaded capability registry resolves literal ids first.
_LITERAL_PROVIDER: dict[str, str] = {
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5": "anthropic",
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "text-embedding-3-small": "openai",
    # In-house rerank + safety-classify models (mock/stub class, no external provider).
    "rerank-mock-v1": "cypherx",
    "classify-stub-v1": "cypherx",
    # Small (≈7-8B) open models — OpenAI-compatible endpoint (BYOK base_url).
    "llama-3.1-8b-instruct": "openai",
    "qwen2.5-7b-instruct": "openai",
    "mistral-7b-instruct": "openai",
}


class ModelRouter:
    """Resolves aliases and dispatches to provider adaptors."""

    def __init__(self, settings: Settings, pool: AsyncConnectionPool | None = None) -> None:
        self._settings = settings
        self._pool = pool
        # Tenant + platform aliases loaded from DB, keyed by (tenant_id|None, alias).
        self._db_aliases: dict[tuple[str | None, str], Resolution] = {}
        self._mock = MockProvider()
        self._providers: dict[str, ProviderAdaptor] = {
            "anthropic": AnthropicProvider(settings.anthropic_api_key),
            "openai": OpenAIProvider(settings.openai_api_key),
        }

    async def load_aliases(self) -> bool:
        """Load model aliases from ``llms.model_aliases`` into the in-process cache.

        Returns True on a successful load (drives ``llms_config_source``).
        """
        if self._pool is None:
            return False
        try:
            from ..db.pool import fetch_aliases

            rows = await fetch_aliases(self._pool)
        except Exception as exc:  # noqa: BLE001 — keep built-in defaults on failure
            logger.warning("alias_load_failed", error=str(exc))
            return False
        for tenant_id, alias, model_id, provider in rows:
            self._db_aliases[(tenant_id, alias)] = Resolution(provider, model_id)
        logger.info("aliases_loaded", rows=len(rows))
        return True

    async def resolve(self, model: str, tenant_id: str) -> Resolution:
        """Resolve a model alias/literal to a (provider, model_id) pair.

        Resolution order (a TENANT alias always wins so a tenant can override a platform
        alias or shadow a seeded literal model of the same name):
          1. tenant-specific alias — cached, else an RLS-scoped DB lookup for THIS tenant
             (tenant aliases are never in the global preload; RLS hides them from it).
          2. platform default alias (DB-loaded cache, then the built-in fallback map).
          3. literal model id — DB-loaded capability registry, then the cold-start map.
        """
        # 1a) tenant alias — fast path from the warm cache (rarely populated: the global
        #     preload can't see tenant rows under RLS, but a prior per-tenant lookup may
        #     have cached it here).
        res = self._db_aliases.get((tenant_id, model))
        if res is not None:
            return res
        # 1b) tenant alias — authoritative per-tenant DB lookup inside the tenant's own
        #     RLS context. This is what makes a UI-created tenant alias actually resolve.
        if self._pool is not None:
            try:
                from ..db.pool import fetch_tenant_alias

                row = await fetch_tenant_alias(self._pool, tenant_id, model)
            except Exception as exc:  # noqa: BLE001 — a lookup failure must not 5xx; fall through
                logger.warning("tenant_alias_lookup_failed", model=model, error=str(exc))
                row = None
            if row is not None:
                resolved = Resolution(row[0], row[1])
                # memoise so repeat calls this refresh-window skip the round-trip
                self._db_aliases[(tenant_id, model)] = resolved
                return resolved
        # 2) platform default alias (DB, then built-in)
        res = self._db_aliases.get((None, model))
        if res is not None:
            return res
        if model in _PLATFORM_ALIASES:
            return _PLATFORM_ALIASES[model]
        # 3) literal model id — DB-loaded capability registry first; the in-code
        #    map is a cold-start fallback only.
        provider = capability_registry.provider_for(model) or _LITERAL_PROVIDER.get(model)
        if provider is not None:
            return Resolution(provider, model)
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"Unknown model or alias '{model}'.",
            status_code=422,
        )

    def provider_for(self, resolution: Resolution) -> ProviderAdaptor:
        """Return the platform-keyed provider adaptor for a resolution (mock when configured).

        This is the BYOK-agnostic path: it always returns the shared, platform-keyed
        adaptor (or the mock). Callers that want tenant BYOK key selection use the async
        :meth:`provider_for_request` instead.
        """
        if self._settings.mock_providers:
            return self._mock
        adaptor = self._providers.get(resolution.provider)
        if adaptor is None:
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"No adaptor registered for provider '{resolution.provider}'.",
            )
        return adaptor

    async def provider_for_request(
        self, resolution: Resolution, tenant_id: str
    ) -> ProviderAdaptor:
        """Return the provider adaptor for a request, injecting a tenant BYOK key if any.

        Resolution order for the API key the provider will use:
          1. the highest-priority active (or in-grace, during a rotation) tenant BYOK key
             for ``resolution.provider`` (``services.byok.resolve_provider_key``), else
          2. the platform key baked into the shared adaptor.

        BYOK-OVERRIDES-MOCK: a tenant that has registered a key for this provider means
        "use the REAL provider with MY key" — so the BYOK key is honored EVEN when
        ``mock_providers`` is the platform default. This gives "keyless by default, real
        the moment you add a key via /v1/keys" (so RAG/Memory go live per-tenant without
        flipping any global flag). Only when there is NO tenant key do we fall back to the
        mock (mock mode) or the platform-keyed adaptor.

        FAIL-OPEN: any BYOK error (disabled, no pool, DB failure, bad envelope) degrades to
        mock/platform — a BYOK problem never fails the request. The resolved secret is never logged.
        """
        # 1) BYOK — the tenant's connection for this provider (secret + base_url + kind).
        resolved: byok.ResolvedKey | None = None
        try:
            resolved = await byok.resolve_provider_key(
                self._pool, tenant_id, resolution.provider, self._settings
            )
        except Exception as exc:  # noqa: BLE001 — BYOK lookup must never 5xx the request
            logger.warning("byok_resolve_failed", provider=resolution.provider, error=str(exc))
            resolved = None

        if resolved is not None:
            metrics.byok_key_source_total.labels("tenant", resolution.provider).inc()
            return self._adaptor_for_connection(resolution.provider, resolved)

        # 2) No tenant connection. PURE BYOK — there is NO platform/env-key fallback. In the
        #    keyless dev default (mock_providers) we return the deterministic mock stub so the
        #    stack runs offline; otherwise we fail with a clear, actionable error.
        if self._settings.mock_providers:
            return self._mock
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            f"No LLM connection configured for provider '{resolution.provider}'. "
            "Add one via POST /v1/keys — there is no platform/env-key fallback.",
        )

    def _adaptor_for_connection(self, provider: str, resolved: byok.ResolvedKey) -> ProviderAdaptor:
        """Build the provider adaptor for a resolved tenant connection, by wire-protocol kind.

        ``anthropic`` -> the native Anthropic adaptor; everything else ('openai',
        'openai_compatible', or any future name) -> the OpenAI-compatible adaptor bound to the
        connection's ``base_url`` (OpenAI / OpenRouter / Together / Groq / vLLM / Ollama /
        self-hosted / future), so a NEW provider needs only a key + base_url, no code.
        """
        kind = (resolved.kind or "openai_compatible").lower()
        if kind == "anthropic":
            base = self._providers.get("anthropic") or AnthropicProvider(None)
            with_creds = getattr(base, "with_credentials", None)
            if callable(with_creds):
                return with_creds(resolved.secret, resolved.base_url)
            return base.with_api_key(resolved.secret)  # type: ignore[attr-defined]
        return OpenAIProvider(resolved.secret, resolved.base_url)
