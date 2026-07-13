"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the Phase 3 K8s spec. Defaults target a
local developer machine so the service boots without a populated environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Memory service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "memory-service"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> memory schema, runtime user mem_user) ────────
    database_url: str = "postgresql://mem_user:localdev@localhost:5432/cypherx_platform"
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

    # ── Embeddings (llms-gateway POST /v1/embeddings) ─────────────────────────
    # The Memory service does NOT embed locally; it calls the llms-gateway embeddings
    # surface (the WP06 blocking deliverable). When the gateway is unreachable (or
    # embeddings_mock_fallback is forced on), a DETERMINISTIC in-process pseudo-embedder
    # is used so the service (and the tests) run with no network. Vector dim is fixed at
    # 1536 (matches the gateway's text-embedding-3-small native dimension).
    embeddings_base_url: str = "http://localhost:8081"
    embeddings_model: str = "embed"
    embeddings_timeout_seconds: float = 5.0
    embeddings_vector_dim: int = 1536
    # Service-to-service token used on the outbound embeddings call (INTERNAL mode). Empty
    # by default -> no Authorization header is sent (local/dev + mock-fallback path).
    embeddings_service_token: str = ""
    # Contract-12 service-token mint (so Memory forwards the CALLER's tenant identity to the
    # llms-gateway embeddings call via X-Forwarded-Agent-JWT and inherits that tenant's BYOK key,
    # exactly like RAG). Empty bootstrap secret => falls back to embeddings_service_token / mock.
    auth_service_url: str = "http://localhost:8080"
    service_principal_name: str = "memory"  # X-Service-Name when minting service tokens
    service_bootstrap_secret: str = ""
    # When True, ALWAYS use the deterministic mock embedder and never touch the network
    # (set in tests + offline dev). When False, the gateway is tried first and the mock
    # is only the fail-open fallback on an unreachable/erroring gateway.
    embeddings_mock_fallback: bool = False

    # ── Store / content bounds ─────────────────────────────────────────────────
    # Hard cap on a single memory's content (16 KiB). Over -> 413 VALIDATION_ERROR.
    content_max_bytes: int = 16 * 1024  # 16 KiB
    # Default near-duplicate cosine-similarity threshold: a NEW store whose nearest
    # same-principal neighbour scores >= this is treated as a duplicate and BUMPS the
    # existing memory instead of inserting a near-copy. Per-tenant override lives in
    # memory.tenant_config.dedup_threshold.
    dedup_threshold: float = 0.95

    # ── Retrieve bounds ────────────────────────────────────────────────────────
    # Hard ceiling on search top_k (a caller may ask for fewer). Bounds the scan +
    # the returned result set so a tenant can never trigger an unbounded read.
    search_top_k_max: int = 50

    # ── TTL sweep (lifespan background job) ─────────────────────────────────────
    # Periodically hard-delete expired memories (expires_at < now) in bounded batches
    # so an accumulation of TTL'd rows can never block on one giant DELETE.
    ttl_sweep_enabled: bool = True
    ttl_sweep_interval_seconds: float = 300.0
    ttl_sweep_batch_size: int = 500

    # ── Quota / usage metering (Auth Contract-19 memory limits) ───────────────
    # Per-principal quota enforcement: memories_max, storage_bytes_max (resource caps,
    # checked on store) and stores_per_min / retrieves_per_min (rate caps, Valkey
    # fixed-window). FAILS OPEN: if the limits cannot be resolved (no claim, DB down)
    # the default permissive tier is used and the request proceeds.
    quota_enabled: bool = True
    quota_key_prefix: str = "cypherx:mem:rl:"
    quota_window_seconds: int = 60
    quota_valkey_timeout_seconds: float = 0.15
    default_plan: str = "free"
    plan_cache_ttl_seconds: float = 60.0

    # ── Retrieval scoring (Stanford "Generative Agents" composite) ───────────────
    # ADDITIVE + DEFAULT-OFF: when False the service ranks search results by pure cosine
    # similarity exactly as today. When True, results are re-ranked by a composite score
    #   composite = w_rec*recency + w_imp*importance + w_rel*relevance
    # over the ANN candidate window (the candidate SET is unchanged; only the ORDER of the
    # returned rows differs). Each component is normalized to [0, 1] before weighting.
    memory_scoring_enabled: bool = False
    memory_scoring_weight_recency: float = 1.0
    memory_scoring_weight_importance: float = 1.0
    memory_scoring_weight_relevance: float = 1.0
    # Recency half-life in seconds (exp decay): score 0.5 at one half-life since last use.
    # 7 days by default — older memories decay but never fully vanish from the composite.
    memory_scoring_recency_half_life_seconds: float = 7 * 24 * 3600.0
    # Optional LLM-graded importance on write (behind its own flag). DEFAULT OFF -> the
    # deterministic length/keyword heuristic is always used (no network, no cost).
    memory_importance_llm_enabled: bool = False

    # ── Contradiction / temporal validity (supersession) ─────────────────────────
    # ADDITIVE + DEFAULT-OFF: when True, a store that conflicts with a prior memory of the
    # SAME principal (high embedding similarity AND lexical-overlap signal, but NOT a
    # dedup-level near-identical copy) marks the prior memory superseded (sets
    # valid_until + superseded_by_id) instead of deleting it. Search then returns only
    # CURRENT (valid) memories by default. DEFAULT OFF keeps today's behavior exactly.
    memory_contradiction_enabled: bool = False
    # Lower bound on cosine similarity for two memories to be considered "about the same
    # thing" (candidate for contradiction). Must be below dedup_threshold so an exact
    # duplicate still dedups (bump) rather than supersedes.
    memory_contradiction_sim_min: float = 0.80
    # When True, search excludes superseded (valid_until <= now / superseded_by_id set)
    # memories. Independent of the contradiction-write flag so a reader can opt in/out.
    memory_search_current_only: bool = True

    # ── Consolidation / forgetting (opt-in background routine; OFF by default) ────
    # ADDITIVE + DEFAULT-OFF: a background job that clusters + summarizes low-importance
    # old memories and SOFT-deletes the originals to an audit trail. NEVER runs unless
    # explicitly enabled. Skeleton with safe defaults; does not run by default.
    memory_consolidation_enabled: bool = False
    memory_consolidation_interval_seconds: float = 24 * 3600.0
    memory_consolidation_min_age_seconds: float = 30 * 24 * 3600.0
    memory_consolidation_max_importance: float = 0.30
    memory_consolidation_batch_size: int = 200

    # ── Usage metering (Contract-19 cypherx.memory.usage.recorded outbox event) ──
    # ADDITIVE: emit a metering event on store/search/delete via the outbox. DEFAULT ON
    # because it is purely additive (a NEW topic; consumers opt in) and fixes the missing
    # Contract-19 usage event. Set False to suppress (e.g. a noisy local loop).
    memory_usage_events_enabled: bool = True

    # ── B1: halfvec / binary quantization for the HNSW ANN scan ──────────────────
    # ADDITIVE + DEFAULT-OFF. Controls the vector type used by the ANN first pass on the
    # Postgres repo (the base vector(1536) column is UNCHANGED — this only picks which
    # index + cast the planner uses):
    #   'off'           (DEFAULT): scan the full-precision vector(1536) HNSW index — today.
    #   'halfvec'       : scan the halfvec(1536) expression HNSW index (2x smaller index,
    #                     near-identical recall); adds a negligible ::halfvec cast per query.
    #   'binary_rerank' : a bit(1536) Hamming first pass, then a full-precision cosine rerank
    #                     of that candidate window (most aggressive; optional second index).
    # The offline in-memory harness runs no SQL, so this knob is inert there (documented gap:
    # true HNSW ANN recall at a given ef_search needs a pgvector-backed eval track).
    memory_vector_quantization: str = "off"  # off | halfvec | binary_rerank

    # ── B2: content-hash embedding cache (Valkey) ────────────────────────────────
    # ADDITIVE + DEFAULT-OFF. When on, embed_many/embed_one key an exact cache on
    # hash(model + dim + text): a HIT returns the cached vector (sub-ms Valkey GET, no
    # gateway round-trip); a MISS embeds once and writes back. The key namespaces model+dim
    # so a model/dim change can NEVER serve a stale vector. FAILS OPEN on any Valkey error
    # (skip the cache, embed normally). Default off => byte-identical to today.
    memory_embedding_cache_enabled: bool = False
    memory_embedding_cache_ttl_seconds: int = 7 * 24 * 3600  # 7 days
    memory_embedding_cache_key_prefix: str = "cypherx:mem:emb:"

    # ── B3: explicit HNSW build params + query-time ef_search ─────────────────────
    # m / ef_construction are ONE-TIME index-build params (migration #5 rebuilds the base
    # HNSW index WITH these values); they mirror pgvector's defaults so a rebuild is
    # behavior-preserving. ef_search is the per-query GUC that bounds how many candidates
    # the HNSW scan returns (pgvector default 40). memory_hnsw_ef_search=0 (DEFAULT) means
    # DO NOT emit `SET LOCAL hnsw.ef_search` — the connection default is used, so the
    # default query path is byte-identical. Raise it toward the oversample window
    # (top_k * oversample) to make the two-pass oversample real and lift recall@k.
    memory_hnsw_m: int = 16
    memory_hnsw_ef_construction: int = 64
    memory_hnsw_ef_search: int = 0  # 0 => do not emit SET LOCAL (preserve pgvector default)

    # ── B4: ACT-R base-level activation (recency x frequency) ────────────────────
    # ADDITIVE + DEFAULT-OFF. The composite's recency term uses exponential decay by
    # default ('exponential'); 'power_actr' switches it to ACT-R base-level activation
    # B = frequency_weight*ln(access_count) - d*ln(age), fusing how RECENTLY and how OFTEN
    # a memory was retrieved (power-law decay). memory_scoring_frequency_weight=0.0 (DEFAULT)
    # means no frequency contribution, so with decay='exponential' the path is unchanged.
    memory_scoring_decay: str = "exponential"  # exponential | power_actr
    memory_scoring_frequency_weight: float = 0.0
    memory_actr_decay_d: float = 0.5  # ACT-R power-law decay exponent (Anderson: ~0.5)

    # ── B5: salient-fact extraction at ingest ────────────────────────────────────
    # ADDITIVE + DEFAULT-OFF. When on, POST /v1/memories decomposes multi-fact content into
    # atomic facts (real deterministic split; optional llms-gateway chat seam) and stores
    # each fact as its own row+embedding. Runs AFTER the content cap + Idempotency-Key
    # short-circuit and BEFORE embedding. Fails soft to storing the raw content. Ingest-only;
    # the read path is untouched. Default off => byte-identical single-row store.
    memory_extraction_enabled: bool = False
    memory_extraction_llm_enabled: bool = False  # use the gateway seam (else heuristic split)
    memory_extraction_max_facts: int = 16  # hard fan-out ceiling per request

    # ── B6: MMR diversity re-rank of the candidate window ────────────────────────
    # ADDITIVE + DEFAULT-OFF. When on, the read re-rank step greedily maximizes
    # lambda*relevance - (1-lambda)*max(sim to already-selected) so the top_k covers
    # distinct facets instead of k near-paraphrases. Query-time only, no extra DB/embed
    # round-trip (needs candidate vectors resident: the PG repo fetches v.embedding ONLY
    # when this flag is on, so the default path stays byte-identical). Fails soft to the
    # existing order. lambda=1.0 == pure relevance, lambda=0.0 == pure diversity.
    memory_mmr_enabled: bool = False
    memory_mmr_lambda: float = 0.5

    # ── B7: associative memory linking + graph-expansion retrieval ───────────────
    # ADDITIVE + DEFAULT-OFF. When on, store() writes explicit edges from a new memory to
    # its nearest associative neighbours (memory.memory_links, RLS-scoped like memories),
    # and search() does a bounded 1-hop, embedding-free link expansion after the ANN
    # oversample (ONE extra DB round-trip, no extra vector/embed call) to surface memories
    # the single-shot cosine missed. Fails soft to the vector-only set. Default off =>
    # byte-identical.
    memory_linking_enabled: bool = False
    memory_linking_max_neighbors: int = 3  # edges written per store
    memory_linking_sim_min: float = 0.50  # min cosine to consider two memories associated
    memory_linking_expansion_limit: int = 10  # max linked rows pulled in at retrieval

    # ── Behaviour toggles ──────────────────────────────────────────────────────
    # When true the embeddings client always uses the deterministic mock embedder
    # (alias of embeddings_mock_fallback for parity with the other services'
    # MOCK_PROVIDERS switch — either env var forces the offline path).
    mock_providers: bool = False

    @property
    def use_mock_embeddings(self) -> bool:
        """True when the deterministic offline embedder must be used unconditionally."""
        return self.embeddings_mock_fallback or self.mock_providers


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
