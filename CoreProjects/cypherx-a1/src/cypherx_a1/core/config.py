"""Application settings (pydantic-settings).

Configuration is read from the process environment with no prefix (Doppler convention);
defaults target a local developer machine so the service boots without a populated env.
Mirrors the xAgent ax-1 settings shape and adds the product-specific knobs (RAG KB names
+ embedding pin, connector config, extraction/retrieval tuning).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the cypherx-a1 product service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "cypherx-a1"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (Neon POOLED -> cypherx_a1 schema, runtime role cxa1_user) ──
    database_url: str = "postgresql://cxa1_user:localdev@localhost:5432/cypherx_platform"
    db_pool_open_at_startup: bool = True

    # ── Kafka (Redpanda) ─────────────────────────────────────────────────────
    kafka_brokers: str = "localhost:9092"
    outbox_publisher_enabled: bool = True

    # ── Valkey (SOFT dependency — never gates readiness) ──────────────────────
    valkey_url: str = "redis://localhost:6379/0"

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"
    auth_service_url: str = "http://localhost:8080"

    # ── Live token revocation — verifier-side MIRROR (Contract 1 / WP03) ──────
    # Shared Valkey kill-switch keys (same prefix Auth + the other verifiers read).
    # FAILS OPEN when Valkey is unreachable (availability wins).
    revocation_key_prefix: str = "cypherx:rev:"
    revocation_check_enabled: bool = True
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Service identity + service-to-service auth (Contract 12) ──────────────
    # The principal name presented to Auth as X-Service-Name; must match Auth's
    # service-auth registration name and the service_acl caller_service exactly.
    service_principal_name: str = "cypherx-a1"
    # Bootstrap secret exchanged at Auth POST /v1/service-tokens. REQUIRED — no baked
    # default, so a missing value fails fast at boot rather than 401-ing downstream calls.
    service_bootstrap_secret: str

    # ── Downstream SharedCore services ────────────────────────────────────────
    llms_gateway_url: str = "http://localhost:8085"
    guardrails_service_url: str = "http://localhost:8086"
    rag_service_url: str = "http://localhost:8087"
    memory_service_url: str = "http://localhost:8088"
    tool_registry_url: str = "http://localhost:8089"

    llms_timeout_seconds: float = 120.0
    guardrails_timeout_seconds: float = 15.0
    rag_timeout_seconds: float = 30.0
    memory_timeout_seconds: float = 15.0

    # ── RAG knowledge bases + embedding pin (Phase-alignment guarantee) ───────
    # Every KB is created with an EXPLICIT pinned model name (never the repointable
    # 'embed' alias) so all KBs share one stable embedding space; dim 1536 is the only
    # platform-supported dimension. Logical KB names; the resolved kb_id is persisted in
    # cypherx_a1.rag_kbs at first use.
    rag_embedding_model: str = "text-embedding-3-small"
    rag_embedding_dim: int = 1536
    rag_kb_code: str = "eng-code"
    rag_kb_conversations: str = "eng-conversations"
    rag_kb_docs: str = "eng-docs"
    rag_kb_incidents: str = "eng-incidents"
    # RAG retrieval clamps (RAG server caps top_k at 100, ef_search at 500).
    rag_query_top_k: int = 20
    rag_query_min_score: float = 0.0
    rag_query_ef_search: int = 100

    # ── Knowledge extraction (LLM) ─────────────────────────────────────────────
    extraction_model: str = "smart"
    # Bumping this SUPERSEDES prior extracted edges (bitemporal valid_to) without
    # re-spending on unchanged content (extraction_jobs ledger keys on it).
    extractor_version: str = "1.0.0"
    extraction_max_tokens: int = 1024
    extraction_temperature: float = 0.0
    # Phase A: a single confidence floor for extracted edges. Below it, an edge is either
    # dropped or kept-but-flagged (metadata.flagged=true) per confidence_floor_mode.
    # 'flag' (default) preserves recall; 'drop' keeps the graph tighter.
    extraction_confidence_floor: float = 0.6
    confidence_floor_mode: str = "flag"  # 'flag' | 'drop'

    # ── Phase KG: knowledge-graph ACCURACY (schema-guided extraction + entity resolution +
    # extraction QA). ALL flags default to TODAY'S behavior — off / no-op — so the verified
    # spine + MVP are byte-for-byte unchanged unless a deployment opts in. ──────────────────
    # Schema/ontology-guided extraction: constrain the extractor to the allowed relation +
    # entity-type set (kg.schema.DEFAULT_SCHEMA). When ON, an off-schema (hallucinated)
    # relation is rejected ('reject') or kept-but-flagged metadata.schema_ok=false ('flag').
    extraction_schema_enabled: bool = False
    extraction_schema_mode: str = "reject"  # 'reject' | 'flag'
    # Extraction QA: record the source span + the extractor's own confidence per edge so a
    # reviewer can audit provenance. No behavioral change when off (today's path writes
    # neither column). When ON the columns are populated from the LLM's evidence/span.
    extraction_span_capture_enabled: bool = False

    # Entity resolution / canonicalization: type-aware coreference so 'J. Smith' / 'John
    # Smith' resolve to one entity; the loser's edges are redirected to the canonical id and
    # the mention is preserved for audit. OFF by default (today only exact handle/email
    # cross-tool identity resolution runs, unchanged). Applies to person + keyed kinds.
    entity_resolution_enabled: bool = False
    # Minimum coreference confidence to auto-merge; below it a candidate is recorded as a
    # mention but NOT merged (a future human-review queue can promote it).
    entity_resolution_min_confidence: float = 0.85

    # ── Phase B: reflection / consolidation pass (Generative-Agents memory win) ────────
    # Cluster recent edges by (target_entity, rel); for high-confidence clusters synthesize a
    # short expertise_summary/capability node + evidence edges (source='consolidation').
    consolidation_version: str = "1.0.0"
    consolidation_avg_confidence: float = 0.75  # min cluster avg confidence to summarize
    consolidation_min_cluster: int = 3  # min edges in a cluster before it is summarized
    consolidation_max_tokens: int = 512
    consolidation_lookback_limit: int = 500  # recent edges scanned per run
    # Scheduled consolidation tick (in addition to the on-demand /v1/extract?consolidate=true).
    consolidation_schedule_enabled: bool = False
    consolidation_interval_seconds: int = 86400

    # ── Copilot ────────────────────────────────────────────────────────────────
    copilot_model: str = "smart"
    copilot_max_tokens: int = 1024
    copilot_temperature: float = 0.2
    # Episodic copilot memory written to memory-service after each answered question.
    copilot_memory_enabled: bool = True
    copilot_memory_type: str = "episodic"

    # ── Hybrid retrieval tuning (graph + RAG-dense + tsvector, RRF fusion) ─────
    retrieval_graph_limit: int = 20
    retrieval_keyword_limit: int = 20
    retrieval_max_hops: int = 3
    # Reciprocal-rank-fusion constant (k in 1/(k+rank)). 60 is the canonical default.
    retrieval_rrf_k: int = 60
    retrieval_context_max_chunks: int = 12

    # ── Phase A: graph-aware rerank (confidence x recency on the fused RRF score) ──────
    # After RRF, each fused item's score is multiplied by
    #   (1 + w_conf * edge_confidence) * ((1 - w_recency) + w_recency * recency_decay)
    # so high-confidence, current edges outrank speculative/stale ones (MemGPT precedence,
    # adapted). w_recency=0 disables the recency term; halflife controls the decay.
    rerank_confidence_weight: float = 1.0
    rerank_recency_weight: float = 0.5
    rerank_recency_halflife_days: float = 90.0

    # ── Phase C: intent-aware retrieval (per-leg RRF weights by query type) ────────────
    # A lightweight regex classifier (no ML) buckets a question into ownership / dependency /
    # expertise / timeline / reasoning / general, then scales each leg's RRF contribution.
    query_type_weighting_enabled: bool = True

    # ── Phase C: Degree-of-Knowledge expertise + ownership concentration ──────────────
    # Recency-decayed expert_in computed from authored (+ lower-weighted reviewed) signal per
    # (person, repo); ownership concentration = Herfindahl over authorship shares (bus-factor).
    expertise_recency_halflife_days: float = 180.0
    expertise_reviewed_weight: float = 0.5
    expertise_version: str = "1.0.0"

    # ── Connector: GitHub (MVP) ────────────────────────────────────────────────
    # mock = replay bundled fixtures (keyless local); live = call the GitHub API.
    connector_mode: str = "mock"
    # Phase B change-tracking granularity: 'auto' (commit-level where the commit stream is
    # available, else PR/ticket), 'commit' (force per-commit change nodes), or 'pr_ticket'
    # (PR + ticket-transition only). Configurable per the user's "both" decision.
    connector_change_granularity: str = "auto"
    github_token: str = ""
    github_webhook_secret: str = "local-dev-webhook-secret"
    github_api_url: str = "https://api.github.com"
    # Bounded per-tick backfill page size (resumable via sync_cursors).
    backfill_page_size: int = 100

    # ── Worker (ingestion/extraction Kafka consumer) ──────────────────────────
    worker_enabled: bool = True
    ingestion_topic_prefix: str = "cypherx.cypherxa1"
    ingestion_consumer_group: str = "cypherx-cypherxa1-workers"
    worker_max_attempts: int = 3

    # ── Usage metering (Contract 19 — app emits its OWN usage on its OWN topic) ──
    usage_topic: str = "cypherx.cypherxa1.usage.recorded"

    # ── OpenTelemetry (Contract 8 — span export OPT-IN; NO-OP unless endpoint set) ──
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "cypherx-a1"
    otel_exporter_otlp_protocol: str = "grpc"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
