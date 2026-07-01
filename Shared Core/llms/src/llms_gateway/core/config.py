"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the Phase 3 K8s spec. Defaults target a
local developer machine so the gateway boots without a populated environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the LLMs gateway."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "llms-gateway"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> llms schema, runtime user llms_user) ─────────
    database_url: str = (
        "postgresql://llms_user:localdev@localhost:5432/cypherx_platform"
    )
    # DB pool sizing (env DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE). Defaults = the prior hardcoded
    # 1/10; raise max_size to lift per-instance throughput (the measured concurrency ceiling).
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # ── Kafka ────────────────────────────────────────────────────────────────
    kafka_brokers: str = "localhost:9092"

    # ── Valkey (SOFT dependency — /readyz reports it but never fails on it) ───
    valkey_url: str = "redis://localhost:6379/0"
    valkey_ping_timeout_seconds: float = 2.0

    # ── DB-authoritative config (aliases / pricing / capabilities) ────────────
    # Periodic in-process registry refresh from Postgres (Component 2, amended).
    config_refresh_interval_seconds: float = 60.0

    # ── Read APIs (WP05 — /v1/usage, /v1/cost) bounds ─────────────────────────
    # Cap aggregation reads so a tenant can never trigger an unbounded scan: the
    # query LIMITs the grouped result set, and an unset/over-wide time window is
    # clamped to the most-recent N days (the GROUP BY makes "rows" = distinct
    # group keys, so these are generous). Both are env-overridable.
    read_max_result_rows: int = 1000
    read_max_range_days: int = 366

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    # In-cluster: http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"

    # ── Token revocation (WP03 — shared verifier-side kill-switch mirror) ──────
    # The verifier mirrors Auth's Valkey revocation keys (jti / kid / agent-epoch)
    # AFTER signature/iss/aud/exp/scope pass. Revocation is defense-in-depth, so the
    # check FAILS OPEN: if Valkey is unavailable the token is ACCEPTED (a slow/dead
    # Valkey must never stall or hard-fail a request). All four services share this
    # key prefix; it must match Auth's REVOCATION_KEY_PREFIX.
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    # Short, independent timeout for revocation lookups so a slow Valkey never adds
    # more than a tick to request latency (the readyz ping uses its own longer one).
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Plan / limits resolution (WP05 — services/auth_client.py) ─────────────
    # Primary plan source is the JWT `plan` claim (no network). The Auth HTTP
    # fallback (`GET {auth_base_url}/v1/tenants/{id}/limits`) is a documented stub
    # until the codebase grows a service-token provider — see auth_client.py. When
    # the claim is absent we fall back to the DB `llms.rate_limits` row for this plan.
    auth_base_url: str = "http://localhost:8080"
    # Default plan tier assumed when neither the JWT claim nor an override resolves a
    # known plan (also the fail-open tier). 'free' is the safe, most-restrictive choice.
    default_plan: str = "free"
    # In-process TTL (seconds) for the resolved plan -> limits cache.
    plan_cache_ttl_seconds: float = 60.0

    # ── Rate limiting (WP05 — services/rate_limit.py) ─────────────────────────
    # Master switch; when false, enforce_pre/debit_tokens are no-ops (still safe to call).
    rate_limit_enabled: bool = True
    # Valkey key prefix for all rate-limit counters. Distinct from the revocation
    # prefix; gateway-local so it does NOT need to match the other services.
    rate_limit_key_prefix: str = "cypherx:llms:rl:"
    # Fixed-window length in seconds for the requests/tokens-per-min counters.
    rate_limit_window_seconds: int = 60
    # Per-command Valkey timeout for rate-limit ops — short so a slow Valkey never
    # adds more than a tick to request latency (fail-open on timeout).
    rate_limit_valkey_timeout_seconds: float = 0.15

    # ── Idempotency (WP05 — services/idempotency.py, Contract-9) ──────────────
    # Master switch; when false, begin/get_replay/complete are no-ops (fail-open).
    idempotency_enabled: bool = True
    # Valkey key prefix for idempotency records.
    idempotency_key_prefix: str = "cypherx:llms:idem:"
    # TTL (seconds) for a stored completed response available for replay. 24h default.
    idempotency_ttl_seconds: int = 86400
    # TTL (seconds) on the initial `in_flight` marker — bounds how long a crashed /
    # never-completed request blocks duplicates before a retry is allowed through.
    idempotency_in_flight_ttl_seconds: int = 300
    # Per-command Valkey timeout for idempotency ops (fail-open on timeout).
    idempotency_valkey_timeout_seconds: float = 0.15

    # ── Embeddings (WP06 — POST /v1/embeddings) bounds ────────────────────────
    # Hard caps on a single embeddings request, enforced BEFORE the provider call so
    # an oversized batch can never reach upstream / blow memory. Both env-overridable.
    #   embeddings_max_input_items : max number of strings in `input` (list form).
    #   embeddings_max_payload_bytes: max total UTF-8 byte size of all input text
    #                                 (25 MiB default — mirrors OpenAI's request ceiling).
    # Over either cap -> 413 VALIDATION_ERROR (Contract-2) before the provider runs.
    embeddings_max_input_items: int = 256
    embeddings_max_payload_bytes: int = 25 * 1024 * 1024  # 25 MiB

    # ── Rerank (POST /v1/rerank) — pluggable provider, default deterministic MOCK ─
    # Provider seam mirrors MOCK_PROVIDERS for embeddings/chat:
    #   RERANK_PROVIDER=mock  (DEFAULT) -> deterministic lexical-overlap scorer, no
    #                          keys / no network / no heavy model deps. Stable offline.
    #   RERANK_PROVIDER=local -> a cross-encoder (bge-reranker class) loaded locally.
    #                          NOT wired into the default image (heavy deps); the seam
    #                          raises a clear SERVICE_UNAVAILABLE until provisioned.
    # Default alias 'rerank-default' resolves to the cypherx mock reranker. Caps below
    # bound a single rerank request BEFORE the provider runs (413 over a cap).
    rerank_provider: str = "mock"
    rerank_default_model: str = "rerank-default"
    # Optional local cross-encoder model id (only consulted when rerank_provider=local).
    rerank_local_model: str = "BAAI/bge-reranker-base"
    rerank_max_documents: int = 256
    rerank_max_payload_bytes: int = 25 * 1024 * 1024  # 25 MiB total UTF-8 (query+docs)

    # ── Safety classify (POST /v1/classify) — default deterministic STUB ──────────
    # Honors the platform-wide CLASSIFIER_MODE default 'stub' (keyword/deterministic):
    #   CLASSIFIER_MODE=stub  (DEFAULT) -> permissive keyword classifier: verdict=allow
    #                          with empty/low category scores unless a deterministic
    #                          keyword rule fires. No model, no keys, no network — today's
    #                          behaviour unchanged.
    #   CLASSIFIER_MODE=local -> a small safety model (Llama Guard / ShieldGemma / Prompt
    #                          Guard class). NOT wired into the default image; the seam
    #                          raises SERVICE_UNAVAILABLE until provisioned.
    classifier_mode: str = "stub"
    classifier_default_model: str = "safety-default"
    # Optional local safety model id (only consulted when classifier_mode=local).
    classifier_local_model: str = "meta-llama/Llama-Guard-3-1B"
    classify_max_input_bytes: int = 1 * 1024 * 1024  # 1 MiB single payload

    # ── Streaming correctness (WP05 chat-path core) ───────────────────────────
    # Hard wall-clock ceiling (seconds) on a single streamed completion. When the
    # provider stream exceeds this we stop consuming, bill the tokens burned so far,
    # and emit a terminal timeout error event. Bounds a hung/slow upstream so an SSE
    # connection can never be held open indefinitely.
    stream_wall_clock_timeout_seconds: float = 120.0

    # ── max_tokens enforcement (WP05) ──────────────────────────────────────────
    # Policy when the requested max_tokens exceeds the model's hard output cap
    # (services/capabilities.py): "reject" -> 400 MAX_TOKENS_EXCEEDED (the plan
    # default), "clamp" -> silently clamp to the cap and emit the
    # X-Cypherx-Param-Clamped: max_tokens response header.
    max_tokens_over_cap_policy: str = "reject"

    # ── Billing-replay journal (WP05, best-effort, optional-infra) ─────────────
    # When the post-completion DB usage write fails (billing_pending), the
    # UsageWrite is appended as one JSON line to an append-only journal file so a
    # replay worker can re-drive it later. Fail-open: a journal write failure only
    # logs. Master switch + path (defaults to a local volume dir).
    billing_journal_enabled: bool = True
    billing_journal_path: str = "/var/lib/llms-gateway/billing-journal.ndjson"

    # ── Provider keys (platform-managed) ──────────────────────────────────────
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # ── BYOK (bring-your-own-key, WP06 — services/byok.py + api/keys.py) ───────
    # Master key-encryption-key (KEK) used to wrap each per-secret AES-256-GCM DEK in a
    # `sealed:v1:` envelope. Read from the env var LLMS_BYOK_KEK (NEVER hardcoded). When
    # EMPTY (the default) BYOK is DISABLED: seal()/registering a sealed key raises, the
    # resolver returns None, and every provider call falls back to the platform key — so
    # the service (and the unit tests) run unchanged without a KEK configured. The KEK is a
    # base64- or hex-encoded 32-byte (256-bit) value, or any passphrase >= 32 bytes (it is
    # HKDF-derived to a 32-byte key in byok.py); see services/byok.py for the exact rule.
    # Accept BYOK_KEK (the actual field name) AND the documented LLMS_BYOK_KEK alias so either
    # env var enables BYOK (the docstring/migration historically named LLMS_BYOK_KEK).
    byok_kek: str = Field(default="", validation_alias=AliasChoices("byok_kek", "BYOK_KEK", "LLMS_BYOK_KEK"))
    # Grace window (days) after a rotation during which BOTH the old (status='rotating')
    # and the new (status='active') key remain selectable, so in-flight callers keyed to
    # the previous upstream secret keep working. The old key's grace_until = now + this.
    byok_grace_days: int = 7

    # ── Per-key ACLs (WP06 — Contract-18, services/acl.py) ────────────────────
    # OPT-IN per-API-key allow-lists (llms.api_key_acls): a key with NO acl rows is
    # UNRESTRICTED (the Contract-18 default). When a key HAS rows, the resolved
    # model/provider/operation must be permitted by >=1 row else 403 FORBIDDEN
    # (reason ACL_DENIED). FAILS OPEN (allow) when the master switch is off, the
    # principal has no api_key_id, the DB pool is absent, or the ACL load errors.
    acl_enabled: bool = True

    # ── Request body-size cap (WP06 — core/body_limit.py middleware) ──────────
    # Hard ceiling on the inbound request body. Over -> 413 PAYLOAD_TOO_LARGE
    # (Contract-2). Enforced via Content-Length AND while streaming the body so a
    # missing/lying Content-Length cannot blow memory. 25 MiB default.
    max_request_body_bytes: int = 25 * 1024 * 1024  # 25 MiB

    # ── Multimodal image caps (WP06 — api/chat.py message validation) ─────────
    # Per chat-completion request: max number of image content parts and the max
    # TOTAL decoded bytes of inline (base64 data-URI) images. Over either -> 413
    # PAYLOAD_TOO_LARGE (Contract-2) BEFORE the provider call. URL (non-inline)
    # image parts do not count toward the byte cap (their bytes aren't in the body).
    max_images_per_request: int = 4
    max_image_bytes: int = 20 * 1024 * 1024  # 20 MiB total inline image bytes

    # ── Image URL handling (WP06 — services/image_fetch.py) ───────────────────
    # DEFAULT = URL pass-through: image_url parts are forwarded to the provider as-is
    # and the fetcher is NEVER invoked. When True, the gateway downloads each image_url
    # to inline base64 via the SSRF-hardened fetcher (scheme/IP allow-list, size cap,
    # timeout, image/* content-type) before forwarding.
    image_inline_required: bool = False
    # SSRF fetcher bounds (only consulted when image_inline_required is True).
    image_fetch_max_bytes: int = 20 * 1024 * 1024  # 20 MiB per fetched image
    image_fetch_timeout_seconds: float = 5.0

    # ── Pricing-staleness watchdog (WP06 — services/pricing_staleness.py) ─────
    # The age (seconds) of the newest llms.provider_pricing.updated_at beyond which the
    # pricing data is considered STALE -> WARN + (optional) webhook alert. Default 7d.
    pricing_staleness_max_age_seconds: float = 7 * 24 * 3600  # 7 days
    # Alert sink. Empty (default) = LOG-ONLY (no webhook POST). Set to an Alertmanager /
    # Slack-relay URL in production. Production ALSO runs the check from a scheduler.
    pricing_staleness_webhook_url: str = ""
    pricing_staleness_webhook_timeout_seconds: float = 3.0

    # ── Behaviour toggles ──────────────────────────────────────────────────────
    # When true the router always selects the deterministic mock provider so the
    # service is runnable with no provider keys / no network.
    mock_providers: bool = False

    # ── Tool-calling emulation (small/8B models) ───────────────────────────────
    # The gateway can EMULATE tool-calling for models that lack a reliable native
    # tools[] function-calling API (model_capabilities.native_tool_use=false): the
    # tool schemas + a strict tool-call protocol are injected into the prompt, the
    # provider is called as a plain chat, and the model's text reply is parsed back
    # into normalized message.tool_calls + finish_reason="tool_calls". This lets
    # EVERY model — small or large — use platform tools through the same `tools`
    # contract. Per-request `tool_mode` (auto|native|emulated) overrides; "auto"
    # (the default) emulates iff the model is known-non-native.
    #   * master switch — OFF makes "auto"/"emulated" behave as "native" (no shim).
    tool_emulation_enabled: bool = True
    #   * "auto" decision for a model with NO capability row (unknown native_tool_use):
    #     default False = treat unknown as native (frontier-safe; don't wrap a model
    #     we can't classify). Set True to emulate unknown models too.
    emulate_tools_when_unknown: bool = False
    # Hard ceiling on the number of tool schemas injected into an emulated prompt
    # (keeps a small model's context + decision space bounded). Extra tools beyond
    # this are dropped from the offered set (the caller should pre-select).
    tool_emulation_max_tools: int = 16


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
