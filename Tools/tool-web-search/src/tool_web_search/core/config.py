"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the Phase 3 K8s spec. Defaults target a
local developer machine so the server boots without a populated environment.

This MCP server is stateless: NO PostgreSQL. Valkey is a SOFT dependency used only
for the fail-open per-tenant rate limiter and idempotency replay — its absence never
fails a request or readiness.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the tool-web-search MCP server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "tool-web-search"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── Valkey (SOFT dependency — /readyz reports it but never fails on it) ───
    valkey_url: str = "redis://localhost:6379/0"
    valkey_ping_timeout_seconds: float = 2.0

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    # In-cluster: http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # ── Token revocation (WP03 — shared verifier-side kill-switch mirror) ──────
    # The verifier mirrors Auth's Valkey revocation keys (jti / kid / agent-epoch)
    # AFTER signature/iss/aud/exp/scope pass. Revocation is defense-in-depth, so the
    # check FAILS OPEN: if Valkey is unavailable the token is ACCEPTED. All services
    # share this key prefix; it must match Auth's REVOCATION_KEY_PREFIX.
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_valkey_timeout_seconds: float = 0.15

    # ── MCP manifest (Contract 4) ─────────────────────────────────────────────
    # Semantic version of the manifest contract schema and the MCP wire-protocol.
    manifest_schema_version: str = "1.0.0"
    manifest_protocol_version: str = "mcp/1.0"
    # Hard per-invocation ceiling (seconds) declared in the manifest; xAgent rejects
    # effective timeouts above this. Also bounds the provider's own httpx call.
    tool_timeout_seconds: int = 30

    # ── Search provider (pluggable; env-selected) ─────────────────────────────
    # 'mock'    — deterministic canned results, NO network (default; local + tests).
    # 'serpapi' — real https://serpapi.com/search.json call (needs SERPAPI_API_KEY).
    # 'brave'   — real Brave Search API call (needs BRAVE_API_KEY).
    search_provider: str = "mock"
    serpapi_api_key: str | None = None
    serpapi_base_url: str = "https://serpapi.com/search.json"
    brave_api_key: str | None = None
    brave_base_url: str = "https://api.search.brave.com/res/v1/web/search"
    # httpx timeout for a real provider call (capped by tool_timeout_seconds upstream).
    provider_timeout_seconds: float = 10.0
    # Bounds on the `max_results` invoke arg (also reflected in the manifest schema).
    default_max_results: int = 5
    max_max_results: int = 20

    # ── Output cap (Contract 4 invoke) ────────────────────────────────────────
    # Hard ceiling on the serialized JSON size of a single invoke result. Over -> the
    # result is REJECTED with 413 PAYLOAD_TOO_LARGE rather than streamed back. 10 MiB.
    max_output_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # ── Per-tenant rate limiting (fail-open Valkey fixed-window) ───────────────
    # Master switch; when false enforce_pre is a no-op. Fixed-window per tenant: when
    # the request count for the current window exceeds `requests_per_min` -> 429 +
    # Retry-After. FAIL-OPEN: any Valkey problem (absent / connect error / timeout) ->
    # ALLOW (availability wins, same posture as the WP03 revocation mirror).
    rate_limit_enabled: bool = True
    rate_limit_requests_per_min: int = 60
    rate_limit_key_prefix: str = "cypherx:tws:rl:"
    rate_limit_window_seconds: int = 60
    rate_limit_valkey_timeout_seconds: float = 0.15

    # ── Idempotency (Contract-9 style; Valkey-backed, fail-open) ──────────────
    # Replay the same invoke result for a repeated Idempotency-Key (per tenant).
    # FAIL-OPEN: any Valkey problem -> proceed without the guarantee (no replay).
    idempotency_enabled: bool = True
    idempotency_key_prefix: str = "cypherx:tws:idem:"
    idempotency_ttl_seconds: int = 86400
    idempotency_valkey_timeout_seconds: float = 0.15

    # ── Request body-size cap (core/body_limit.py middleware) ─────────────────
    # Hard ceiling on the inbound request body. Over -> 413 PAYLOAD_TOO_LARGE. 1 MiB
    # is generous for a search query payload.
    max_request_body_bytes: int = 1 * 1024 * 1024  # 1 MiB


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
