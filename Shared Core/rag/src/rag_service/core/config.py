"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the K8s spec. Defaults target a local
developer machine so the service boots without a populated environment. NOTHING is
hardcoded — every operationally-meaningful value is an env-overridable field here.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known platform tenant (Contract 13) — owner of the platform-skills KB. A
# constant UUID, never a row in any rag.* table (RAG does not own tenant lifecycle).
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# Vector dimension for the first-cycle embedding model (text-embedding-3-small). The
# only per-dimension vector table shipped is chunk_vectors_1536.
DEFAULT_EMBEDDING_DIM = 1536


class Settings(BaseSettings):
    """Runtime configuration for the RAG service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "rag-service"
    service_version: str = "0.1.0"
    service_principal_name: str = "rag"  # X-Service-Name when minting service tokens
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> rag schema, runtime user rag_user, pgvector) ──
    database_url: str = "postgresql://rag_user:localdev@localhost:5432/cypherx_platform"
    # DB pool sizing (env DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE). Defaults = the prior hardcoded
    # 1/10; raise max_size to lift per-instance throughput (the measured concurrency ceiling).
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # ── Kafka ────────────────────────────────────────────────────────────────
    kafka_brokers: str = "localhost:9092"

    # ── Valkey (SOFT dependency — /readyz reports it but never fails on it) ───
    valkey_url: str = "redis://localhost:6379/0"
    valkey_ping_timeout_seconds: float = 2.0

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"
    auth_service_url: str = "http://localhost:8080"
    # Contract-12 bootstrap secret used to mint outbound service tokens (rag -> llms).
    service_bootstrap_secret: str = "local-dev-rag-secret"

    # ── Token revocation (WP03 — shared verifier-side kill-switch mirror) ──────
    # Mirrors Auth's Valkey revocation keys (jti / kid / agent-epoch) AFTER
    # signature/iss/aud/exp/scope pass. FAILS OPEN: an unavailable Valkey ACCEPTS the
    # token (defense-in-depth). Prefix must match Auth's REVOCATION_KEY_PREFIX.
    revocation_check_enabled: bool = True
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_valkey_timeout_seconds: float = 0.15

    # ── LLMs gateway (embeddings — POST /v1/embeddings) ───────────────────────
    # The embeddings dependency. When unreachable / mock_embeddings is on, the
    # in-process deterministic mock vectors are used so tests + local need no network.
    llms_gateway_url: str = "http://localhost:8085"
    llms_timeout_seconds: float = 30.0
    # Force the deterministic in-process mock embedder (no network, no service token).
    # Tests pin this true; local can flip it on to run with no llms-gateway.
    mock_embeddings: bool = False
    # When a real llms call fails AND this is true, fall back to the deterministic mock
    # vector rather than 5xx the request (keeps query/ingest resilient locally).
    embeddings_fallback_to_mock: bool = True

    # ── Embedding model resolution (KB creation + bootstrap fallback) ──────────
    # The literal model the `embed` alias resolves to when the llms GET /v1/models call
    # is unreachable. Pinned env vars per the Component-10 lazy-bootstrap amendment so the
    # bootstrap can never deadlock on the LLMs soft-dependency (circular cold start).
    embedding_model_alias: str = "embed"
    embedding_model_resolved: str = "text-embedding-3-small"
    embedding_dim: int = DEFAULT_EMBEDDING_DIM

    # ── Knowledge base / query bounds ─────────────────────────────────────────
    # top_k server cap (over -> VALIDATION_ERROR). ef_search default + cap (HNSW recall).
    query_top_k_cap: int = 100
    hnsw_ef_search_default: int = 100
    hnsw_ef_search_cap: int = 500

    # ── Hybrid retrieval (dense + lexical + RRF) — ADDITIVE, default off ────────
    # The default search_mode is 'dense' (the two-pass HNSW CTE is untouched). When a query
    # opts into search_mode='hybrid' (or 'sparse'), the lexical leg uses
    # websearch_to_tsquery + ts_rank_cd over rag.chunks.content_tsv (migration 0003) and the
    # two rankings are fused with Reciprocal Rank Fusion. These knobs only take effect on the
    # hybrid/sparse paths.
    hybrid_rrf_k: int = 60  # RRF constant (standard k=60); rank contribution 1/(k+rank).
    # Per-leg candidate depth pulled before fusion (each leg returns ~this many rows). Bounded
    # so a hybrid query never scans the whole KB; clamped by hybrid_candidate_cap.
    hybrid_candidate_multiplier: int = 10  # candidates per leg ≈ top_k * this
    hybrid_candidate_cap: int = 200

    # ── Rerank (optional cross-encoder rerank via llms-gateway /v1/rerank) ──────
    # Default OFF — when off, ``rerank=true`` on a query is a no-op (today's behaviour). When
    # enabled, an opted-in query retrieves a wider candidate pool (rerank_candidate_n) then
    # asks the gateway to re-order them, returning the top_k. Mock-tolerant: a failed/missing
    # gateway falls back to the pre-rerank ordering when rerank_fallback_to_base is on.
    rag_rerank_enabled: bool = False
    rerank_candidate_n: int = 150  # top-N pulled before reranking
    rerank_model: str = "rerank"  # llms-gateway rerank model alias
    rerank_fallback_to_base: bool = True  # on gateway failure, keep the base ordering
    # Force the deterministic in-process mock reranker (no network/service token). Inherits
    # mock_embeddings when unset-equivalent so keyless local dev reranks without a gateway.
    mock_rerank: bool = False

    # ── Contextual ingest (chunk-context generation via llms-gateway chat) ──────
    # Default OFF — ingest is byte-for-byte unchanged. When enabled, each chunk gets a 1-2
    # sentence context generated from the document, prepended before embedding AND folded into
    # the tsvector text (via metadata.context). Mock-tolerant + fail-soft (a gateway failure
    # falls back to the raw chunk so ingest never breaks).
    rag_contextual_ingest: bool = False
    contextual_model: str = "chat"  # llms-gateway chat model alias for context generation
    contextual_max_doc_chars: int = 4000  # doc prefix sent as grounding (cost guard)
    contextual_max_context_chars: int = 320  # cap on the generated context string

    # ── Query decomposition (multi-hop retrieval via llms-gateway chat) ────────
    # Default OFF — when off, ``decompose=true`` on a query is a no-op (today's behaviour). When
    # enabled, an opted-in compound query is split into ≤ decompose_max_subquestions focused
    # sub-questions; the handler retrieves per sub-question, unions+dedups by chunk_id, and feeds
    # the merged pool to the (already-gated) rerank stage. Mock-tolerant + fail-soft: any gateway
    # failure (or a non-decomposable query) degrades to the original single-query retrieval.
    rag_decompose_enabled: bool = False
    decompose_max_subquestions: int = 4  # hard cap on sub-questions per query
    decompose_model: str = "chat"  # llms-gateway chat model alias for decomposition

    # ── Multi-query expansion / RAG-Fusion (via llms-gateway chat) ─────────────
    # Default OFF — when off, ``multi_query=true`` on a query is a no-op (today's behaviour). When
    # enabled, the query is rewritten into multiquery_num_variants paraphrases; the handler
    # retrieves per variant and fuses the ranked lists with application-level Reciprocal Rank
    # Fusion (hybrid_rrf_k). A RECALL lever for vocabulary mismatch; pair with rerank to keep
    # top-k precision. Mock-tolerant + fail-soft: any gateway failure degrades to single-query.
    rag_multiquery_enabled: bool = False
    multiquery_num_variants: int = 3  # generated paraphrases (in addition to the original query)
    multiquery_model: str = "chat"  # llms-gateway chat model alias for expansion

    # ── Inline ingest ─────────────────────────────────────────────────────────
    # Hard cap on inline text content bytes (over -> VALIDATION_ERROR).
    inline_max_bytes: int = 100 * 1024  # 100 KiB
    # Pre-signed upload size cap (server validates BEFORE generating a URL).
    upload_max_bytes: int = 100 * 1024 * 1024  # 100 MiB

    # ── Chunking ──────────────────────────────────────────────────────────────
    default_chunking_strategy: str = "sentence"  # fixed | sentence
    default_chunk_size: int = 512
    default_chunk_overlap: int = 50

    # ── Embedding batch policy (worker -> llms /v1/embeddings) ────────────────
    # Deliberately under the gateway's 256-item / 25 MiB hard caps for retry headroom.
    embed_batch_max_items: int = 128
    embed_batch_max_bytes: int = 8 * 1024 * 1024  # 8 MiB serialized

    # ── Object storage (MinIO/S3 — env-driven; bucket name never a schema literal) ──
    s3_bucket: str = "cypherx-rag-local"
    s3_endpoint: str = "http://localhost:9000"
    # Credentials from env S3_ACCESS_KEY / S3_SECRET_KEY (match the MinIO container creds; the
    # local default is the keyless throwaway 'cypherxlocal', NOT a real secret). Previously the
    # object store hardcoded 'minioadmin' with no override, which never matched the running MinIO.
    s3_access_key: str = "cypherxlocal"
    s3_secret_key: str = "cypherxlocal"
    s3_sse_mode: str = "none"  # none | kms — 'none' against MinIO first cycle
    s3_kms_key_id: str = ""  # only used when s3_sse_mode == 'kms'
    presign_expiry_seconds: int = 900
    # Allowed content types for the presigned-upload path.
    upload_content_type_allowlist: str = "application/pdf,text/markdown,text/plain"

    # ── Idempotency (Contract 9 — /ingest/finalize) ───────────────────────────
    idempotency_enabled: bool = True
    idempotency_key_prefix: str = "cypherx:rag:idem:"
    idempotency_ttl_seconds: int = 86400  # 24h
    idempotency_in_flight_ttl_seconds: int = 300
    idempotency_valkey_timeout_seconds: float = 0.15

    # ── Quota enforcement (Auth Contract-19 limits) ───────────────────────────
    # Resolved from the JWT plan/limits like the other services. FAILS OPEN when a plan
    # cannot be resolved (availability wins). 413 on storage/count breach, 429 on rate.
    quota_enabled: bool = True
    quota_key_prefix: str = "cypherx:rag:quota:"
    quota_window_seconds: int = 60
    quota_valkey_timeout_seconds: float = 0.15
    default_plan: str = "free"
    plan_cache_ttl_seconds: float = 60.0

    # ── Platform-skills bootstrap (Component 10 — lazy-with-retry) ────────────
    bootstrap_enabled: bool = True
    bootstrap_kb_name: str = "platform-skills"
    bootstrap_retry_seconds: float = 30.0

    # ── Background sweepers ────────────────────────────────────────────────────
    s3_deletion_sweep_interval_seconds: float = 30.0
    s3_deletion_batch_size: int = 200

    # ── Ingestion worker (Kafka consumer) ─────────────────────────────────────
    # Disabled by default so an API-only pod (and the test suite) never spin a
    # consumer; the dedicated worker process / compose service flips it on.
    worker_enabled: bool = False
    ingestion_topic: str = "cypherx.rag.ingestion.requested"
    ingestion_consumer_group: str = "cypherx-rag-ingestion-workers"
    worker_max_attempts: int = 3  # attempts before DLQ (poison-pill flow)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
