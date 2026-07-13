"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention. Defaults target a local developer machine so the
service boots without a populated environment.

Unlike the stateless tool-web-search scaffold this fork was based on, the Flow-Tool-Bridge
IS stateful: it owns the ``flow_tools`` Postgres schema (workflow->tool bindings + per-tenant
Node-RED runtimes). Valkey stays a SOFT dependency (fail-open rate limit + idempotency).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the tool-flow-bridge service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "tool-flow-bridge"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> flow_tools schema, runtime user flow_tools_user) ──
    database_url: str = (
        "postgresql://flow_tools_user:localdev@localhost:5432/cypherx_platform"
    )
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # ── Valkey (SOFT dependency — /readyz reports it but never fails on it) ───
    valkey_url: str = "redis://localhost:6379/0"
    valkey_ping_timeout_seconds: float = 2.0

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # ── Token revocation (WP03 — shared verifier-side kill-switch mirror) ──────
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Contract-12 service-token acquisition (bridge -> Auth -> registry) ─────
    # The bridge mints a short-lived SERVICE JWT to call the Tool Registry on behalf
    # of the publishing user (INTERNAL auth mode). tenant_id + tool:admin scope come
    # from the forwarded user agent-JWT, never the service token.
    auth_service_url: str = "http://localhost:8080"
    service_principal_name: str = "tool-flow-bridge"
    service_bootstrap_secret: str = "local-dev-flowbridge-secret"

    # ── Tool Registry (where published flows are registered/discovered) ───────
    tool_registry_url: str = "http://localhost:8089"
    registry_timeout_seconds: float = 5.0

    # ── MCP manifest (Contract 4) ─────────────────────────────────────────────
    manifest_schema_version: str = "1.0.0"
    manifest_protocol_version: str = "mcp/1.0"
    # Hard per-invocation ceiling (seconds) declared in the generated manifest and used
    # to bound the outbound Node-RED webhook call.
    tool_timeout_seconds: int = 30

    # ── This bridge's own reachable base URL (drives each tool's manifest base_url) ──
    # In-cluster: http://tool-flow-bridge.tools.svc.cluster.local:8080. Each published
    # workflow is registered with base_url = {bridge_base_url}/w/<slug>.
    bridge_base_url: str = "http://tool-flow-bridge:8080"

    # ── Node-RED runtime (the execution backend) ──────────────────────────────
    # Header the bridge sends to a workflow's HTTP-In endpoint; the flow's http-in node
    # (or its front `Function`) checks it so ONLY the bridge can trigger the workflow.
    nodered_invoke_secret_header: str = "X-CypherX-Tool-Secret"
    nodered_admin_timeout_seconds: float = 8.0
    nodered_invoke_timeout_seconds: float = 30.0
    # Header the bridge sends to Node-RED's Admin API as the bearer token.
    nodered_admin_scheme: str = "Bearer"
    # Editor + Admin-API root (httpAdminRoot). Set to the SAME path the browser sees through
    # the BFF (``/bff/nodered``) so Node-RED's editor asset URLs resolve when iframed behind
    # the BFF proxy (no HTML/JS URL rewriting; sidesteps node-red#986). MUST differ from the
    # HTTP-In root (http_node_root, default /flow) so admin routes and node routes never collide.
    nodered_admin_root: str = "/bff/nodered"

    # ── Tenant-runtime provisioner ────────────────────────────────────────────
    # 'static'     — a single shared dev Node-RED wired via env (compose/local).
    # 'docker'     — one container per tenant via the Docker Engine API (dev multi-tenant).
    # 'kubernetes' — one Deployment+PVC+Service+NetworkPolicy per tenant (production).
    provisioner_mode: str = "static"
    # Static-mode wiring (dev): every tenant resolves to this one Node-RED instance.
    static_nodered_internal_host: str = "http://nodered:1880"
    static_nodered_http_node_root: str = "/flow"
    static_nodered_admin_token: str = "local-dev-nodered-admin-token"
    static_nodered_invoke_secret: str = "local-dev-nodered-invoke-secret"
    static_nodered_credential_secret: str = "local-dev-nodered-credential-secret"
    # Kubernetes-mode wiring (production).
    nodered_image: str = "cypherx/nodered-tenant:local"
    nodered_namespace: str = "cypherx-tools"
    nodered_service_domain: str = "tools.svc.cluster.local"
    nodered_container_port: int = 1880
    nodered_cpu_limit: str = "500m"
    nodered_memory_limit: str = "512Mi"
    nodered_storage_size: str = "1Gi"
    nodered_runtime_class: str | None = None  # e.g. "gvisor" for strong isolation
    # Untrusted tenant flow code runs here, so the container rootfs is read-only by default
    # (only the /data PVC + an ephemeral /tmp are writable). Toggle off ONLY if a runtime genuinely
    # needs to write elsewhere and it crash-loops.
    nodered_read_only_root_fs: bool = True
    # Explicit egress allow-list (CIDRs) the tenant Node-RED NetworkPolicy permits.
    # Empty => deny all egress except DNS. Platform service CIDRs are NEVER added here.
    nodered_egress_allow_cidrs: str = ""
    # If an allow CIDR is the catch-all 0.0.0.0/0, these ranges are subtracted (ipBlock.except) so a
    # broad allow can NEVER reach the cloud metadata endpoint or internal/RFC-1918 networks — the
    # primary SSRF / lateral-movement guard. Only applied to 0.0.0.0/0 (except must be within cidr).
    nodered_egress_block_cidrs: str = (
        "169.254.169.254/32,169.254.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    )

    # ── Platform (public) Node-RED runtime (Phase 5 · 5-bridge) ────────────────
    # A SINGLETON, platform-owned Node-RED that hosts PUBLIC (promoted) tools. Distinct from the
    # per-tenant egress-DENY runtimes: it egresses to external search providers (egress-ALLOW) and
    # holds a platform provider-key credential. Static mode (local/compose) reuses the single shared
    # dev Node-RED; docker/k8s address a dedicated `nodered-platform` instance.
    platform_nodered_name: str = "nodered-platform"
    static_platform_nodered_internal_host: str = "http://nodered:1880"
    static_platform_nodered_admin_token: str = "local-dev-nodered-admin-token"
    static_platform_nodered_invoke_secret: str = "local-dev-nodered-invoke-secret"
    static_platform_nodered_credential_secret: str = "local-dev-nodered-credential-secret"
    # Egress-ALLOW: the platform runtime MAY reach external providers. Defaults to the catch-all with
    # nodered_egress_block_cidrs subtracted (metadata + RFC-1918), so it still can NEVER reach the
    # cloud metadata endpoint or internal platform services (same SSRF guard as the tenant path).
    platform_nodered_egress_allow_cidrs: str = "0.0.0.0/0"

    # ── Editor session (BFF iframe proxy target) ──────────────────────────────
    editor_session_ttl_seconds: int = 3600

    # ── Publishing ────────────────────────────────────────────────────────────
    # Default access mode applied to a newly published tool when the publisher does not
    # choose one. 'ask' = human-in-the-loop approval per invocation (safe-by-default).
    default_access_mode: str = "ask"

    # ── web_search public-tool bootstrap (Phase 5 · 5-websearch) ───────────────
    # Identity the one-shot bootstrap CLI (`python -m tool_flow_bridge.services.bootstrap`)
    # publishes + promotes the PUBLIC web_search flow-tool AS. The user JWT is forwarded to the
    # Tool Registry (X-Forwarded-Agent-JWT) and MUST carry tool:admin + tenant:admin +
    # platform:admin (promote is the platform-only path to visibility='public'). Empty by default so
    # the running service ignores them — they are read only by the bootstrap CLI.
    bootstrap_tenant_id: str = ""
    bootstrap_agent_id: str = ""
    bootstrap_user_jwt: str = ""
    # NOTE: the public web_search flow selects its provider from the PLATFORM Node-RED runtime's OWN
    # env (SEARCH_PROVIDER / SERPAPI_API_KEY / BRAVE_SEARCH_API_KEY, delivered by the
    # nodered-platform-secrets Secret — see charts/nodered-platform). No provider key here in the
    # bridge; an unset key => deterministic keyless MOCK results. So no new secret ref is needed.

    # ── Invoke authorization ──────────────────────────────────────────────────
    # Per-tool authorization is governed by the Tool Registry's per-agent ACCESS GRANT
    # (none|ask|automated), the same model the Publish dialog + the agent's tool-access
    # toggles write. On invoke the bridge resolves the calling agent's access for the tool and
    # DENIES only an explicit 'none'. Fail-OPEN on any registry error — xAgent already enforces
    # access fail-closed before it ever calls, so a registry blip must not break live tools.
    enforce_registry_access: bool = True

    # ── Output cap (Contract 4 invoke) ────────────────────────────────────────
    max_output_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # ── Per-tenant rate limiting (fail-open Valkey fixed-window) ───────────────
    rate_limit_enabled: bool = True
    rate_limit_requests_per_min: int = 120
    rate_limit_key_prefix: str = "cypherx:tfb:rl:"
    rate_limit_window_seconds: int = 60
    rate_limit_valkey_timeout_seconds: float = 0.15
    # A separate, tighter limit on the expensive publish path.
    publish_rate_limit_per_min: int = 20

    # ── Idempotency (Contract-9 style; Valkey-backed, fail-open) ──────────────
    idempotency_enabled: bool = True
    idempotency_key_prefix: str = "cypherx:tfb:idem:"
    idempotency_ttl_seconds: int = 86400
    idempotency_valkey_timeout_seconds: float = 0.15
    # TTL of the in-flight lock that stops two concurrent invokes with the same Idempotency-Key
    # from BOTH dispatching to a side-effecting flow. Sized above the invoke timeout so the lock
    # outlives the dispatch, with a safety margin; it auto-expires if the process dies mid-flight.
    idempotency_lock_ttl_seconds: int = 60

    # ── Request body-size cap (core/body_limit.py middleware) ─────────────────
    max_request_body_bytes: int = 4 * 1024 * 1024  # 4 MiB (flow args can be larger)

    @property
    def egress_allow_cidr_list(self) -> list[str]:
        return [c.strip() for c in self.nodered_egress_allow_cidrs.split(",") if c.strip()]

    @property
    def egress_block_cidr_list(self) -> list[str]:
        return [c.strip() for c in self.nodered_egress_block_cidrs.split(",") if c.strip()]

    @property
    def platform_egress_allow_cidr_list(self) -> list[str]:
        return [c.strip() for c in self.platform_nodered_egress_allow_cidrs.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
