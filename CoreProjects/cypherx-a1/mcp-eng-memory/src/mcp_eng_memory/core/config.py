"""Settings (pydantic-settings, no prefix)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    service_name: str = "mcp-eng-memory"
    service_version: str = "1.0.0"
    environment: str = "local"

    # Auth (Contract 1)
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # Revocation mirror (shared kill-switch; fail-open)
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_valkey_timeout_seconds: float = 0.15
    valkey_url: str = "redis://localhost:6379/0"

    # Backing product API
    cypherxa1_base_url: str = "http://localhost:8093"
    backend_timeout_seconds: float = 60.0

    # MCP manifest (committed source of truth)
    manifest_path: str = "./manifest.json"
    server_name: str = "mcp-eng-memory"
    coarse_scope: str = "tool:invoke"
    fine_scope: str = "tool:mcp-eng-memory:invoke"

    # Caps (Contract-4 platform standard)
    max_request_body_bytes: int = 1_048_576
    max_output_bytes: int = 10_485_760


@lru_cache
def get_settings() -> Settings:
    return Settings()
