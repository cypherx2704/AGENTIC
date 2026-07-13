"""Application settings (pydantic-settings).

All configuration is read from the process environment (no prefix), matching the
Doppler-injected env-var convention from the Phase 9 K8s spec. Defaults target a
local developer machine so the runtime boots without a populated environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the xAgent agent-runtime."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Service identity ─────────────────────────────────────────────────────
    service_name: str = "agent-runtime"
    service_version: str = "0.1.0"
    environment: str = "local"

    # ── PostgreSQL (PgBouncer -> xagent schema, runtime user xagent_user) ──────
    database_url: str = "postgresql://xagent_user:localdev@localhost:5432/cypherx_platform"
    # Master enable flag for opening the connection pool at startup (default ON). Opening
    # spawns a background worker holding a libpq socket; that worker does not yield to
    # asyncio cancellation, so under test it wedges the function-scoped event-loop
    # teardown. Tests set this OFF (the pool is nulled after startup anyway); prod keeps
    # it ON so /readyz reflects real Postgres connectivity.
    db_pool_open_at_startup: bool = True

    # ── Kafka ────────────────────────────────────────────────────────────────
    kafka_brokers: str = "localhost:9092"
    # Master enable flag for the background outbox publisher (default ON). When OFF the
    # lifespan never starts the aiokafka producer task — events still land durably in
    # xagent.outbox, they are simply not drained in-process. Lets an operator run the
    # runtime with an external drainer, AND lets the test-suite avoid a real aiokafka
    # producer (whose connect/teardown over many function-scoped event loops can wedge).
    outbox_publisher_enabled: bool = True

    # ── Valkey (SOFT dependency — cache / signals foundations, WP02) ──────────
    # Used (later) for the agent-config cache, cancellation signals, the authorize
    # -verdict cache and Idempotency-Key replay. WP02 lands only the wiring: a lazy
    # async client, a /readyz soft-report and the valkey_up gauge. Never gates readiness.
    valkey_url: str = "redis://localhost:6379/0"

    # ── Agent-config Valkey read-through cache (Component 1, WP08) ─────────────
    # The LOAD stage resolves the agent's runtime row via a Valkey read-through cache
    # (services.agent_config_cache) keyed by agent_id, so the hot path avoids a Postgres
    # round-trip on every task. SOFT: a cache miss / absent or unreachable Valkey FAILS
    # OPEN to a live DB read (correctness over latency — a cache outage never fails a
    # task). PUT /v1/agents/{id}/runtime busts the key on every config change.
    #   * enabled flag (default ON) — OFF makes LOAD read straight through Postgres;
    #   * TTL 5min (300s) so a stale entry self-heals even if an invalidate is missed;
    #   * key prefix env-overridable (xAgent-private); the agent_id is appended to it.
    agent_config_cache_enabled: bool = True
    agent_config_cache_ttl_seconds: int = 300
    agent_config_cache_key_prefix: str = "cypherx:xagent:agentcfg:"
    # Short Valkey IO budget (seconds) for the agent-config cache get/set/delete round-trips
    # on the task hot path — a slow Valkey must fail fast (fail-open to DB), never stall.
    agent_config_cache_valkey_timeout_seconds: float = 0.25

    # ── Task lifecycle: cooperative cancel + idempotency + authorize cache (WP08) ──
    # Key prefix for the per-task Valkey keys (cancel signals, idempotency replay,
    # authorize-verdict cache). Env-overridable; the default is the shared cluster
    # default namespace for xAgent's own task-lifecycle keys (DISTINCT from the shared
    # cross-service revocation prefix above — these keys are xAgent-private).
    task_signal_key_prefix: str = "cypherx:xagent:task:"
    # Short Valkey IO budget (seconds) for the cancel/idempotency/authorize round-trips
    # on the request hot path — a slow Valkey must fail fast, not stall the request.
    task_signal_valkey_timeout_seconds: float = 0.25
    # TTL (seconds) for a cancel signal key — long enough that a still-running task always
    # observes it between stages, short enough that the key self-evicts after the task ends.
    cancel_signal_ttl_seconds: int = 900

    # ── Async task mode + SSE streaming (WP12) ─────────────────────────────────────
    # Async mode (POST /v1/tasks ?mode=async / body mode=async) runs the pipeline in a
    # fire-and-forget background task and returns 202 + the task_id immediately; polling is
    # via GET /v1/tasks/{id}. Master enable flag — OFF makes an async request 422 (the
    # endpoint behaves sync-only), so an operator can disable the background path per env.
    async_mode_enabled: bool = True
    # Idempotency-Key is REQUIRED for async (a crashed-worker retry must be safe): a missing
    # key on an async submit is a 400 VALIDATION_ERROR. The WP08 sweeper provides crash
    # recovery (a background task that dies leaves a non-terminal row -> swept to failed).

    # SSE streaming (GET /v1/tasks/{id}/stream). Master enable flag — OFF makes the stream
    # endpoint 404 (the feature is simply absent). The endpoint relays the task's
    # step/stage progress + terminal result as Server-Sent Events: it first tries the
    # Valkey Pub/Sub channel (live frames the pipeline publishes) and FALLS BACK to polling
    # the task row + steps when Pub/Sub is unavailable (Valkey absent/erroring), so SSE
    # still works degraded with no infra.
    sse_streaming_enabled: bool = True
    # Poll cadence (seconds) for the SSE FALLBACK path: how often it re-reads the task row +
    # steps to emit a progress snapshot when Pub/Sub is unavailable. Short enough to feel
    # live, long enough to spare the DB. Also the heartbeat cadence on the Pub/Sub path.
    sse_poll_interval_seconds: float = 1.0
    # Hard ceiling (seconds) on a single SSE connection: the relay emits a terminal
    # ``error`` (timeout) frame + closes if the task has not finished within this window, so
    # a wedged task never holds the stream open forever. Defaults above the task ceiling.
    sse_max_duration_seconds: int = 1800
    # Short Valkey IO budget (seconds) for an SSE event PUBLISH round-trip (the subscribe
    # side blocks on listen() and is not bounded by this — the connection lifetime is).
    sse_publish_valkey_timeout_seconds: float = 0.25

    # ── Per-task execution timeout (WP08 — the in-process asyncio.timeout guard) ──
    # The pipeline run is wrapped in asyncio.timeout(task_timeout_seconds); on expiry the
    # task is marked timeout/failed and the terminal event is emitted. This is the default
    # ceiling; the per-request TaskRequest.timeout_seconds (in [1,900]) overrides it when
    # smaller (we never exceed the caller's stated budget).
    task_timeout_seconds: int = 120
    # Cooperative cancel poll cadence is "between stages" (no timer); no separate config.

    # ── Idempotency (Contract 9 — POST /v1/tasks, Idempotency-Key header) ─────────
    # Master enable flag. When OFF the endpoint never consults Valkey for idempotency
    # (the no-Valkey-configured / unit case is auto-disabled regardless — see below).
    idempotency_enabled: bool = True
    # TTL (seconds) for a stored idempotency record (in_flight reservation + completed
    # replay payload). 24h matches the common Contract-9 replay window.
    idempotency_ttl_seconds: int = 86400

    # ── Auth layer-B authorize (task:execute) + Valkey-cached verdict (WP08) ──────
    # Master enable flag for the per-submission authorize check. OFF skips the call
    # entirely (e.g. an environment where Kong/edge already enforces task:execute).
    authorize_enabled: bool = True
    # The action string checked at Auth POST /v1/authorize for task submission.
    authorize_action: str = "task:execute"
    # Cached-verdict TTL (seconds): a suspended tenant stops within this window. 60s per
    # the WP08 plan — short enough that revocation propagates fast, long enough to spare
    # Auth a call per task on the hot path.
    authorize_cache_ttl_seconds: int = 60
    # IO budget (seconds) for the Auth /v1/authorize round-trip.
    authorize_timeout_seconds: float = 2.0

    # ── Backup sweeper (services/sweeper.py — lifespan-scheduled, fail-soft) ───────
    # Master enable flag for the in-process backup sweeper loop. OFF leaves the
    # asyncio.timeout guard as the only timeout mechanism (and disables retention here —
    # an external CronJob can own it instead). Tests leave it OFF (no DB).
    sweeper_enabled: bool = True
    # How often the sweeper wakes (seconds).
    sweeper_interval_seconds: int = 30
    # A non-terminal task whose timeout_at is older than NOW() - this grace (seconds) is
    # swept to failed (covers a crashed worker that never ran the in-process timeout).
    sweeper_stuck_grace_seconds: int = 60
    # Max rows the sweeper finalises per wake (bounded work per tick).
    sweeper_batch_limit: int = 100
    # Retention: delete PUBLISHED outbox rows older than this many days.
    outbox_retention_days: int = 7
    # Retention: delete task_steps older than this many days.
    task_steps_retention_days: int = 90

    # ── Live token revocation — verifier-side MIRROR (Component 3c, WP03) ──────
    # xAgent re-verifies the inbound agent JWT (defense in depth) AND mirrors Auth's
    # shared Valkey kill-switch: after signature/iss/aud/exp/scope pass, reject a token
    # whose jti is revoked, whose signing kid is poisoned, or whose agent has an epoch
    # newer than the token's iat. The key prefix MUST match Auth + the other verifiers
    # (llms / guardrails) — all four read the SAME keys, so this is env-overridable but
    # the default is the shared cluster default. Revocation is a defense-in-depth kill
    # -switch: it FAILS OPEN (accept + log + metric) when Valkey is unreachable, so a
    # Valkey outage can never lock every agent out (availability wins).
    revocation_key_prefix: str = "cypherx:rev:"
    # Master enable flag for the verifier-side revocation check (default ON). Lets an
    # operator disable the mirror per-environment without a code change if Valkey churn
    # ever needs to be taken off the hot path; OFF means tokens are never revocation
    # -checked here (signature/claims verification is unaffected).
    revocation_check_enabled: bool = True
    # Valkey lookup budget for the revocation check (seconds). Deliberately short so a
    # slow/unreachable Valkey degrades to fail-open quickly and never stalls a request.
    revocation_valkey_timeout_seconds: float = 0.15

    # ── Auth / JWKS (Contract 1) ──────────────────────────────────────────────
    # In-cluster: http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json
    auth_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    auth_issuer_url: str = "http://localhost:8080"
    auth_platform_audience: str = "cypherx-platform"
    # Base URL of the Auth service (service-token minting + GET /v1/agents/{id}).
    auth_service_url: str = "http://localhost:8080"

    # ── LLM finish_reason validation (WP02 amended fix) ───────────────────────
    # Comma-separated set of finish_reason values the llms-gateway can legitimately
    # return (the gateway's unified FinishReason enum). Env-overridable so a gateway
    # enum addition needs no xAgent code change; this in-code default is the documented
    # last-resort fallback mirroring the gateway's enum at the time of writing.
    llm_known_finish_reasons: str = "stop,length,tool_calls,content_filter,budget_exceeded"

    # ── Pipeline stage-enable flags (STAGE_ENABLE_<NAME> env vars, WP02) ────────
    # Consulted by the stage registry at startup (core.pipeline.apply_stage_flags) so
    # future stages can be enabled per-environment without code edits. Defaults mirror
    # the first-cycle registry: enhancement stages stay OFF until their phases land.
    stage_enable_load: bool = True
    stage_enable_pre_guardrail: bool = True
    stage_enable_memory_retrieve: bool = False  # 📋 Phase 6
    stage_enable_rag_query: bool = False  # 📋 Phase 5
    stage_enable_skill_load: bool = False  # 📋 Phase 8
    stage_enable_prompt_build: bool = True
    stage_enable_llm: bool = True
    # Phase 7 — ENABLED by default: the stage self-skips (no client call, no audit row) for any
    # agent with empty allowed_tools or tool_loop_enabled=false, so a toolless task is unchanged
    # (still exactly 3 audit rows). Only tool-configured agents run the LLM<->MCP loop. Set
    # STAGE_ENABLE_TOOL_LOOP=false to force it off for an environment.
    stage_enable_tool_loop: bool = True
    stage_enable_post_guardrail: bool = True
    stage_enable_memory_write: bool = False  # 📋 Phase 6

    # ── Downstream services (first-cycle dependencies) ─────────────────────────
    # Local host-shell recipe ports: llms=8085, guardrails=8086 (8081/8082 are taken by
    # redpanda's schema-registry/pandaproxy). In-cluster, inject the service-DNS URLs
    # (e.g. http://llms-gateway:8080) via env/Doppler.
    llms_gateway_url: str = "http://localhost:8085"
    guardrails_service_url: str = "http://localhost:8086"

    # ── Downstream services (WP12 — RAG / Memory / Tool-Registry / MCP tools) ──────
    # The enhancement-cycle dependencies that the RAG, Memory, Tool-Loop and Skill
    # stages call through their per-service clients (services/rag_client.py,
    # memory_client.py, registry_client.py, mcp_client.py). All identity flows via
    # HEADERS (Contract 12 service token + X-Forwarded-Agent-JWT), never the body.
    # Local host-shell recipe ports; in-cluster inject the service-DNS URLs via Doppler.
    #
    # RAG service — knowledge-base retrieval (POST /v1/kbs/{kb_id}/query). A 403
    # FORBIDDEN_KB (ACL deny) is surfaced as a typed `forbidden` RagResult, NOT an
    # exception, so the RAG stage can skip the KB gracefully instead of failing the task.
    rag_service_url: str = "http://localhost:8087"
    rag_timeout_seconds: float = 15.0

    # Memory service — episodic/semantic memory search + store
    # (POST /v1/memories/search, POST /v1/memories). Used by the memory-retrieve /
    # memory-write stages.
    memory_service_url: str = "http://localhost:8088"
    memory_timeout_seconds: float = 15.0

    # Tool Registry — tool manifest + invoke-URL resolution (GET /v1/tools,
    # GET /v1/tools/{name}). The registry client caches each resolved manifest behind a
    # 5-min ETag cache (If-None-Match; a 304 re-uses the cached entry and refreshes its
    # TTL). On a transport/5xx error a still-fresh cached entry is served stale
    # (fail-soft) rather than failing the resolve.
    tool_registry_url: str = "http://localhost:8089"
    registry_timeout_seconds: float = 10.0
    # ETag manifest cache TTL (seconds). Key = "{name}@{version|'latest'}". After the TTL
    # the client re-validates with If-None-Match; a 304 reuses the body + resets the TTL.
    registry_manifest_cache_ttl_seconds: int = 300
    # How many times the registry client retries a transport/5xx error before serving a
    # stale cached entry (if any) or raising. A 4xx is NEVER retried.
    registry_retry_attempts: int = 2

    # Skill Registry — skill manifest + per-agent access resolution (mirror of the Tool
    # Registry, `skills` schema). The SKILL_LOAD stage resolves the agent's allowed_skills
    # and each skill's access mode (none|ask|automated) here, then splices the permitted
    # skills into the prompt. Local host port 8095; in-cluster inject http://skill-registry:8080.
    skill_registry_url: str = "http://localhost:8095"
    skill_registry_timeout_seconds: float = 10.0

    # MCP tool invocation (invoke_url resolved from the registry, e.g. tool-web-search):
    # GET {invoke_url}/manifest, then real MCP at {invoke_url}/mcp (initialize -> tools/call).
    # The client guards each (endpoint, agent) pair with a circuit breaker, retries on
    # connection-error/5xx but NEVER on a 4xx, and stamps an Idempotency-Key from (task_id, tool_call_id).
    mcp_timeout_seconds: float = 30.0
    # Retries on a connection error / 5xx (a 4xx is terminal and never retried). The
    # initial attempt plus this many retries = total attempts.
    mcp_retry_attempts: int = 2
    # Circuit breaker: open after this many CONSECUTIVE failures per (endpoint, agent);
    # stay open (fast-fail SERVICE_UNAVAILABLE) for the cooldown, then allow one half-open
    # trial whose success closes the breaker and whose failure re-opens it.
    mcp_circuit_breaker_threshold: int = 5
    mcp_circuit_breaker_cooldown_seconds: float = 30.0

    # ── Enhancement-stage behaviour (RAG / Memory / Tool-loop / prompt budget, WP12) ──
    # These tune the NEW enhancement stages (rag_query / memory_retrieve / memory_write /
    # tool_loop) and the PROMPT_BUILD context budget. The stages themselves are gated by
    # the per-agent config (allowed_kb_ids / allowed_tools / memory_scope) AND the
    # STAGE_ENABLE_<NAME> flags above — these keys only shape WHAT a running stage does.
    #
    # RAG query stage: a hard ceiling on the per-KB top_k actually requested (the agent's
    # rag_top_k_per_kb is clamped to this so a mis-configured agent can't fan a huge query
    # out to the RAG service). The RAG service contract caps top_k at 20.
    rag_query_max_top_k: int = 20
    # Memory retrieve stage: how many memories to pull into the prompt context (top_k).
    memory_retrieve_top_k: int = 5
    # Memory write stage: store an interaction memory after a successful task. OFF makes
    # the (enabled) MEMORY_WRITE stage retrieve-only — it never writes. Per-agent
    # memory_scope == 'none' also disables the write regardless of this flag.
    memory_write_enabled: bool = True
    # The memory ``type`` recorded for an interaction memory (Memory-service taxonomy).
    memory_write_type: str = "episodic"

    # Tool-loop stage: the iterative LLM<->tool loop bound. After this many LLM turns that
    # still request tools, the loop stops and records a ``tool_loop_limit`` audit/step row
    # (the partial answer is returned, not an error). Bounds run-away tool chatter.
    tool_loop_max_iterations: int = 10
    # Multi-call budget: the maximum number of tool INVOCATIONS across the whole task. On
    # the (max+1)th the task short-circuits BUDGET_EXCEEDED (a distinct, harder cap than
    # the per-turn iteration limit — it bounds total tool side-effects + spend).
    tool_loop_max_invocations: int = 20
    # The xagent.outbox topic each tool invocation is metered to (one event per invoke).
    tool_metering_topic: str = "cypherx.agent.tools.invocation.metered"

    # ── Tool-loop: small-model (≈8B) robustness ────────────────────────────────────────
    # Small models reliably use tools only when the offered set is small and they are
    # explicitly told to. These knobs make the loop work for weak models without harming
    # strong ones; the gateway separately EMULATES tool-calling for non-native models
    # (model_capabilities.native_tool_use=false) so the loop is identical either way.
    #
    # Cap on tools OFFERED to the model per task: xAgent ranks the agent's allowed_tools
    # by relevance to the user message and offers only the top N (0 = offer all). Shrinks
    # the decision space an 8B model must reason over. The gateway applies its own cap on
    # top of this for the emulated prompt (tool_emulation_max_tools).
    tool_loop_max_offered_tools: int = 8
    # Prepend a concise "use a tool when it helps" system nudge to the tool-loop prompt.
    # Weak models often answer from priors instead of calling an available tool; the nudge
    # corrects that. Harmless for strong models (they already gate tool use well).
    tool_loop_tool_use_nudge: bool = True
    # Per-request tool-calling strategy forwarded to the gateway (auto|native|emulated).
    # None (default) omits the field, so the gateway decides per-model via `auto`. Set
    # "emulated" to force prompt-based tool-calling regardless of the model's capability.
    tool_loop_tool_mode: str | None = None

    # ── Human-in-the-loop (Phase 6) — ask-mode tool/skill approval gate ────────────────
    # Enables the HIL client wiring. When OFF (or the client is unwired) an ``ask``-mode tool
    # is DENIED (it never auto-runs). Polling cadence + max wait bound how long an agent task
    # blocks awaiting a human verdict (keep max_wait below TASK_TIMEOUT_SECONDS).
    hil_enabled: bool = True
    hil_poll_interval_seconds: int = 2
    hil_max_wait_seconds: float = 90.0

    # ── Prompt-context budget (PROMPT_BUILD splice + truncation, WP12) ─────────────────
    # The spliced RAG/memory/tool context may consume at most this FRACTION of the agent's
    # token_budget_per_task. When the assembled context exceeds it, PROMPT_BUILD truncates
    # in the order RAG -> memory -> skills (least-to-most agent-authored) and records a
    # ``context_truncated`` step. The system prompt + user message are NEVER truncated.
    prompt_context_budget_fraction: float = 0.30
    # Heuristic chars-per-token divisor used to estimate token cost of spliced context
    # without a tokenizer dependency on the hot path (≈4 chars/token for English). Env
    # -overridable so a deployment with a different tokenizer profile can tune it.
    prompt_context_chars_per_token: int = 4

    # ── OpenTelemetry tracing (Contract 8 — OTLP span export, WP08) ───────────────
    # xAgent always propagates W3C trace context (traceparent + tracestate) on every
    # downstream call (see core.trace). OTel SPAN EXPORT is OPT-IN and DISABLED by
    # default: the exporter starts ONLY when otel_exporter_otlp_endpoint is set to a
    # non-empty collector URL AND the opentelemetry SDK is installed. With the endpoint
    # unset (the default), tracing is a complete NO-OP — local/test runs need no
    # collector and incur no overhead. In-cluster, inject e.g.
    # OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability:4317 via Doppler.
    otel_exporter_otlp_endpoint: str = ""
    # Logical service name attached to exported spans (resource attribute service.name).
    otel_service_name: str = "agent-runtime"
    # OTLP transport protocol: 'grpc' (default, :4317) or 'http/protobuf' (:4318). Only
    # consulted when the endpoint is set; the matching exporter package must be installed.
    otel_exporter_otlp_protocol: str = "grpc"

    # ── Service identity + service-to-service auth (Contract 12) ─────────────────
    # The service principal name presented to Auth as X-Service-Name; must match Auth's
    # service-auth registration name and the service_acl caller_service exactly.
    service_principal_name: str = "xagent"
    # Bootstrap secret exchanged at Auth POST /v1/service-tokens for a short-lived (5-min)
    # service JWT. REQUIRED — no baked default: inject SERVICE_BOOTSTRAP_SECRET via env/Doppler
    # so a missing/placeholder secret fails fast at boot rather than silently 401-ing every
    # downstream guardrails/LLM call. Locally it must equal Auth's configured value.
    service_bootstrap_secret: str

    def known_finish_reasons(self) -> frozenset[str]:
        """Parse ``llm_known_finish_reasons`` into the validated finish_reason set."""
        return frozenset(
            part.strip() for part in self.llm_known_finish_reasons.split(",") if part.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
