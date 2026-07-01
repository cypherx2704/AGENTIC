"""Model capability registry (Component 2 — ``llms.model_capabilities``, amended).

The DB is the single authority for model capabilities: rows are loaded from
Postgres at startup and refreshed every ``config_refresh_interval_seconds`` by the
lifespan refresh loop (see ``main``). The in-code ``_FALLBACK_CAPABILITIES`` map
below is a documented LAST-RESORT COLD-START FALLBACK ONLY (Amendment Log 2026-06)
— it mirrors the seed migration exactly (a test parses the seed SQL and asserts
equality so the two can never drift) and is consulted only until the first
successful DB load. The ``llms_config_source`` gauge reports which source is live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    provider: str
    max_tokens_cap: int
    context_window: int
    supports_vision: bool = True
    supports_tools: bool = True
    supports_streaming: bool = True
    embedding_dim: int | None = None
    # Whether the model/provider supports the NATIVE tools[] function-calling API
    # reliably. False => the gateway EMULATES tool-calling in the prompt (small/8B
    # models). Defaults True so an unknown model is assumed native (frontier-safe).
    native_tool_use: bool = True


# Cold-start fallback ONLY — mirrors db/migrations seed (never authoritative once
# the DB has loaded; kept in lockstep by tests/test_config_registry.py).
_FALLBACK_CAPABILITIES: dict[str, ModelCapability] = {
    "claude-opus-4-8": ModelCapability("claude-opus-4-8", "anthropic", 32000, 200000),
    "claude-sonnet-4-6": ModelCapability("claude-sonnet-4-6", "anthropic", 8192, 200000),
    "claude-haiku-4-5": ModelCapability("claude-haiku-4-5", "anthropic", 8192, 200000),
    "gpt-4o": ModelCapability("gpt-4o", "openai", 16384, 128000),
    "gpt-4o-mini": ModelCapability("gpt-4o-mini", "openai", 16384, 128000),
    # WP06 embeddings model: no completion output (max_tokens_cap sentinel 1), 8191
    # input-token context, no vision/tools/streaming, native 1536-dim vectors.
    "text-embedding-3-small": ModelCapability(
        "text-embedding-3-small",
        "openai",
        max_tokens_cap=1,
        context_window=8191,
        supports_vision=False,
        supports_tools=False,
        supports_streaming=False,
        embedding_dim=1536,
    ),
    # Rerank + safety-classify (cypherx mock/stub class): no completion output
    # (max_tokens_cap sentinel 1), no vision/tools/streaming, not an embedding model.
    "rerank-mock-v1": ModelCapability(
        "rerank-mock-v1",
        "cypherx",
        max_tokens_cap=1,
        context_window=8192,
        supports_vision=False,
        supports_tools=False,
        supports_streaming=False,
    ),
    "classify-stub-v1": ModelCapability(
        "classify-stub-v1",
        "cypherx",
        max_tokens_cap=1,
        context_window=8192,
        supports_vision=False,
        supports_tools=False,
        supports_streaming=False,
    ),
    # Small (≈7-8B) open models served via an OpenAI-compatible endpoint (BYOK
    # base_url). They CAN use tools, but not via the native API reliably, so
    # native_tool_use=False routes them through the gateway's tool-calling emulation.
    "llama-3.1-8b-instruct": ModelCapability(
        "llama-3.1-8b-instruct",
        "openai",
        max_tokens_cap=4096,
        context_window=128000,
        supports_vision=False,
        supports_tools=True,
        supports_streaming=True,
        native_tool_use=False,
    ),
    "qwen2.5-7b-instruct": ModelCapability(
        "qwen2.5-7b-instruct",
        "openai",
        max_tokens_cap=8192,
        context_window=32768,
        supports_vision=False,
        supports_tools=True,
        supports_streaming=True,
        native_tool_use=False,
    ),
    "mistral-7b-instruct": ModelCapability(
        "mistral-7b-instruct",
        "openai",
        max_tokens_cap=4096,
        context_window=32768,
        supports_vision=False,
        supports_tools=True,
        supports_streaming=True,
        native_tool_use=False,
    ),
}


class CapabilityRegistry:
    """In-process cache of ``llms.model_capabilities`` (DB-authoritative)."""

    def __init__(self) -> None:
        self._cache: dict[str, ModelCapability] = dict(_FALLBACK_CAPABILITIES)
        self._loaded_from_db = False

    @property
    def loaded_from_db(self) -> bool:
        return self._loaded_from_db

    async def load_from_db(self, pool: AsyncConnectionPool) -> bool:
        """Refresh the cache from Postgres. Returns True on a successful load."""
        try:
            from ..db.pool import fetch_capabilities  # local import to avoid cycle

            rows = await fetch_capabilities(pool)
        except Exception as exc:  # noqa: BLE001 — keep current cache on any failure
            logger.warning("capabilities_load_failed", error=str(exc))
            return False
        for (
            model_id,
            provider,
            max_cap,
            ctx,
            vision,
            tools,
            streaming,
            emb_dim,
            native_tools,
        ) in rows:
            self._cache[model_id] = ModelCapability(
                model_id=model_id,
                provider=provider,
                max_tokens_cap=int(max_cap),
                context_window=int(ctx),
                supports_vision=bool(vision),
                supports_tools=bool(tools),
                supports_streaming=bool(streaming),
                embedding_dim=int(emb_dim) if emb_dim is not None else None,
                native_tool_use=bool(native_tools),
            )
        self._loaded_from_db = True
        logger.info("capabilities_loaded", rows=len(rows))
        return True

    def get(self, model_id: str) -> ModelCapability | None:
        return self._cache.get(model_id)

    def list_model_ids(self) -> list[str]:
        """Return all known literal model ids (DB-loaded, else cold-start fallback)."""
        return list(self._cache.keys())

    def provider_for(self, model_id: str) -> str | None:
        """Return the provider for a literal model id, or None if unknown."""
        cap = self._cache.get(model_id)
        return cap.provider if cap is not None else None

    def native_tool_use(self, model_id: str) -> bool | None:
        """Return whether ``model_id`` supports NATIVE tool-calling, or None if unknown.

        None lets the caller apply its own default for an unrecognised model (the
        gateway treats unknown-by-default as native unless configured otherwise).
        """
        cap = self._cache.get(model_id)
        return cap.native_tool_use if cap is not None else None


# Process-wide instance (same pattern as services.cost.cost_calculator).
capability_registry = CapabilityRegistry()
