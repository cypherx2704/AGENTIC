"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention. Defaults target a local developer machine so
the service boots without a populated environment. NOTHING is hardcoded at a call
site — every tunable (timeouts, failure thresholds, discovery caps) lives here and
is env-overridable.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Tool Registry service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "tool-registry"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> tools schema, runtime user tool_user) ────────
    database_url: str = "postgresql://tool_user:localdev@localhost:5432/cypherx_platform"

    # ── Valkey (SOFT dependency — /readyz reports it but never fails on it) ───
    valkey_url: str = "redis://localhost:6379/0"
    valkey_ping_timeout_seconds: float = 2.0

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # ── Token revocation (WP03 — shared verifier-side kill-switch mirror) ──────
    # The verifier mirrors Auth's Valkey revocation keys (jti / kid / agent-epoch)
    # AFTER signature/iss/aud/exp pass. Revocation is defense-in-depth, so the check
    # FAILS OPEN: if Valkey is unavailable the token is ACCEPTED. This prefix must
    # match Auth's REVOCATION_KEY_PREFIX (shared across all services).
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Tool registration (WP11) ──────────────────────────────────────────────
    # Max number of ACTIVE versions retained per tool. On a new version that pushes
    # the count over this, the OLDEST active version is retired (status='retired').
    max_active_versions_per_tool: int = 3

    # ── Manifest health poll (WP11) ───────────────────────────────────────────
    # Background loop interval (seconds): GET each tool's /manifest with If-None-Match.
    health_poll_interval_seconds: float = 30.0
    # Per-request timeout for a single manifest poll (fail-soft on timeout).
    health_poll_timeout_seconds: float = 5.0
    # Consecutive-failure thresholds for the health state machine:
    #   active -> degraded once failures >= degrade_after
    #   degraded -> offline once failures >= offline_after
    # A single success resets the counter and returns the tool to 'active'.
    health_degrade_after: int = 1
    health_offline_after: int = 3

    # ── Discovery (WP11) ──────────────────────────────────────────────────────
    # Hard cap on rows returned by GET /v1/tools so a tenant can never trigger an
    # unbounded scan.
    discovery_max_tools: int = 500


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
