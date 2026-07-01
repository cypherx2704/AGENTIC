# First-Cycle 100% — Execution Plan (generated from the 6-auditor plan audit, 2026-06)

Authoritative backlog for completing Auth/LLMs/Guardrails/xAgent to 100% of their phase plans,
plus minimal RAG/Memory/Tools and the production frontend. Amendments in plan-fixes.json are the
decided plan corrections and OVERRIDE the original phase docs where they conflict.

## Work packages (dependency-ordered)

### WP01 — Plan & contract reconciliation
*Service:* archive/Manoj/phases + contracts/  |  *Depends on:* none
- Apply every plan_fix amendment to the phase docs (single editing pass; the decided fixes above are the change list)
- Author missing contract artifacts: contracts/guardrails/jailbreak-leak-patterns.md, contracts/guardrails/golden-suite.jsonl (seeded from existing guardrails test fixtures), contracts/billing/guardrails-rule-cost.md, contracts/api/reserved-metadata-keys.md
- Pin Contract-15 gating: cases 1-10 gate the spine, 11-15 gate WP12/WP14; add Contract-3 optional input.session_id; Contract-9 header registry (Idempotent-Replay spelling); Contract-20 circuit-breaker text
- Rewrite stale ⚡/📋 checklists (guardrails promotions, llms dual-listed items, xAgent MCP relabel) and split all checklists into service-code vs deploy-target sections with compose-parity equivalents
*Test scope:* Doc review + CI lint that golden-suite.jsonl parses and contract artifacts cross-reference cleanly; no service code

### WP02 — Cross-service foundations: Valkey, DB-driven config, auth outbox, billing-key + live-bug fixes
*Service:* all four built services  |  *Depends on:* WP01
- Valkey wiring (VALKEY_URL setting, async client, soft-dep health metric) in llms, guardrails, xagent — prerequisite for everything downstream
- llms: llm_call_id billing-key migration (UNIQUE on llm_call_id, request_id correlation-only, no hot-path ON CONFLICT), DB-authoritative config (llms.model_capabilities table, 60s refresh loop for aliases/pricing/capabilities, literal maps demoted to fixtures, seed reconciliation: opus id, embed/code/vision aliases)
- guardrails: DB rule-metadata overlay at startup (registry rows finally read, readiness fails on code↔DB mismatch), X-Request-ID UUID validation + fallback in trace middleware
- xagent live fixes: body.agent_id == jwt.agent_id enforcement (422), finish_reason validation, agent status enforcement in LOAD, honest non-terminal GET projection, tasks.metadata column, user_id JWT-sub fallback removed, env-driven stage-enable flags
- auth: transactional outbox + relay for tenant lifecycle / token.revoked / policy.changed, event fidelity (agent.updated real topic, pending_deletion vs deleted), secret-redaction logging filter + CI fixture test
*Test scope:* All 142 existing tests stay green; new unit+integration: multi-call billing uniqueness, UUID-fallback persistence with junk header, config-refresh pickup without restart, agent_id mismatch 422, outbox relay durability under Kafka outage

### WP03 — Auth completion I: quotas, limits API, self-protection, revocation enforcement, key rotation
*Service:* Shared Core/auth (+ thin middleware in llms/guardrails/xagent)  |  *Depends on:* WP02
- Quota API: GET/PUT /v1/admin/tenants/{id}/quotas, GET /v1/quotas, GET /v1/tenants/{id}/limits (Contract-19 read consumed by llms/guardrails/rag/memory/tools), effective-limits merge, plan_changed invalidation event
- Quota enforcement inside Auth (agents_max, api_keys_per_agent_max, tokens_issued_per_min via Valkey counters)
- Self-protection rate limits: auth.rate_limit_config DDL + seed, Valkey fixed-window filter on authorize/token/service-tokens/admin, fail-open with in-process hard ceiling
- Verifier-side revocation: jti-revoked + kid-poisoned Valkey checks + bloom prime in Auth verify path AND mirror middleware in llms/guardrails/xagent; key-revoke→jti cascade + agent-deactivate revoke-all; revoked-token purge job
- Signing-key rotation: admin rotate + emergency-rotate endpoints (platform:admin + emergency-token file gate), scheduled verifying→retired job; rotation runbook rehearsed
- Audit coverage for all issuance events (token/service_token/key/agent/oauth/tenant ops); behavior_policies shadow seed row; tenants.plan column + JWT claim; token-mint platform-scoped key_hash lookup (X-Tenant-ID optional)
*Test scope:* Kotlin unit+integration; cross-service E2E: revoke a token → 401 at all four services within seconds; quota 429s; rotation rehearsal incl. old-kid verification window; audit-row presence assertions per issuance path

### WP04 — Auth completion II: onboarding, webhooks, audit pipeline, agent-registry completion
*Service:* Shared Core/auth + new webhook-delivery worker  |  *Depends on:* WP03
- Onboarding per amended 1c: signup/verify/resend/upgrade/close, signup_attempts flow, pluggable SMTP emitter (mailhog locally) + pluggable captcha (mock provider), velocity risk scoring, tenant + first-agent + key creation on verify, tenant.created emission via outbox
- Webhooks: auth.webhook_subscriptions + webhook_deliveries DDL, CRUD endpoints (create/list/delete/rotate-secret/resume/replay/deliveries), signed-delivery worker as own compose service (Contract-21 signing/retry)
- Audit pipeline: GET /v1/audit-log/export (JSONL to minio, presigned, 7d TTL), audit S3 mirror via outbox topic + consumer, hourly chain-verify job
- GET /v1/usage rollup: cypherx.llms.usage.recorded consumer → auth.tenant_usage_counters
- Agent registry completion: GET /v1/agents (paginated list), PATCH /v1/agents/{id} (scopes/capabilities/metadata), DELETE/deactivate + cascade; API key rotation with 24h dual-validity grace; Contract-2 error codes for landed features
*Test scope:* Integration with mailhog + minio; webhook retry/replay matrix; usage-rollup consumer against llms topic fixtures; signup→verify→first-task E2E; agent list/patch/deactivate + cascade revocation tests

### WP05 — LLMs completion I: idempotency, rate limiting, read APIs, streaming correctness, billing journal
*Service:* Shared Core/llms  |  *Depends on:* WP02, WP03
- Contract-9 idempotency on chat (+embeddings later): Valkey in_flight→completed, replay header, 409, fail-open + counter; streams recorded-but-replay-exempt per the decided rule
- Rate limiting: llms.rate_limits table + plan-tier seed, per-tenant fixed window, post-hoc tokens_per_min debit, 429 + Retry-After, fail-open + counter; plan tier from Auth JWT claim/limits endpoint cached 60s
- GET /v1/models (per-tenant resolved aliases), GET /v1/usage + GET /v1/cost (group_by model/agent/api_key/date)
- Streaming correctness: tool-call aggregation on BOTH providers (final tool_calls event), finish_reason + cache-token normalization in streams, mid-stream error → usage row + outbox for tokens burned, 120s wall-clock timeout, client-disconnect cancellation + usage
- max_tokens ceiling via model_capabilities → 400 MAX_TOKENS_EXCEEDED; X-Cypherx-Param-Clamped header; billing-replay journal (local volume, replay worker, readyz depth gates); outbox backoff + 7d purge; auth_client for /v1/authorize tenant-state checks
*Test scope:* Expanded pytest: SSE golden tests (tool-call aggregation, finish_reason map, mid-stream error billing), idempotency replay/conflict/fail-open, rate-limit window + token debit, journal crash-replay, usage/cost aggregation correctness

### WP06 — LLMs completion II: embeddings, BYOK, per-key ACLs, multimodal caps (RAG/Memory critical path)
*Service:* Shared Core/llms  |  *Depends on:* WP05
- POST /v1/embeddings: OpenAI + MOCK embedding providers, 256-item/25MiB caps, dimensions param, usage_records with parametrized operation, Idempotency-Key honored — THE blocking deliverable for RAG and Memory
- Seeds: 'embed' platform alias + text-embedding-3-small pricing rows (output cost 0 convention)
- BYOK: llms.providers + llms.secret_backends registry, sealed:v1 envelope encryption under env KEK + env: prefix, POST /v1/keys register/rotate with 7-day grace, priority-ordered key selection in router
- Contract-18 per-key ACLs: llms.api_key_acls keyed by Auth api_key_id, enforced before resolve()
- Multimodal: 25MiB body-size middleware, 4-image/20MiB caps, URL pass-through default, config-gated SSRF-hardened fetcher for inline-required policy mode; pricing-staleness scheduled check (GitLab pipeline / scheduler container, log+webhook sink)
*Test scope:* Embeddings contract tests (shape, batching caps, idempotent re-call, usage/pricing rows, mock provider); BYOK seal/unseal + KEK rotation + grace window; ACL deny; body-cap 413s; SSRF fetcher unit corpus when flag on

### WP07 — Guardrails completion: policy authoring, simulation, custom rules, perf + detoxify
*Service:* Shared Core/guardrails  |  *Depends on:* WP02, WP03
- Policy CRUD per amended plan: create, append-only edit (version chain + atomic agent_policies repoint), agent assignment, save-time rule/stream_mode validation, audited fail_mode_override
- Simulation: POST /v1/policies/{id}/simulate + inline-draft variant, evaluation_trace mode in pipeline, as_of historical resolution, usage operation='simulate' cost 0, sim/hour limits
- Custom rules (type=regex + classifier-threshold): CRUD, dynamic loader so DB rules actually execute, ReDoS guard (compile + 64KiB/50ms test), versioned update/retire, custom_rules_max quota, golden-suite CI gate
- Hot path: Valkey policy cache 60s; per-tenant rate limiting + byte quota (atomic Lua, fail-closed) against Auth limits; post-response persistence queue + backlog metric replacing fail-soft inline writes
- Redaction key lifecycle: DB-backed resolver (tenant_redaction_keys finally read), rotate endpoint, 30d pending grace + retirement job, pluggable key_ref scheme; per-rule cost column + real cost_usd + output_bytes
- detoxify: slim/ml image targets, baked pinned checkpoint, eager lifespan load, env threshold; startup stream-mode fail-fast + async-mode 422; empty-tenant-policy honored; GET /v1/violations read API (frontend dependency); outbox purge; SLO histogram buckets + Prometheus alert rules
*Test scope:* Expanded pytest: golden-suite regression, ReDoS rejection corpus, policy version-chain + simulation trace, cache staleness, persistence-queue overflow semantics, rate-limit fail-closed, detoxify ml-image smoke with CLASSIFIER_MODE=detoxify, violations API pagination/RLS

### WP08 — xAgent completion I: reliability layer (cancel, timeout, idempotency, authorize, runtime CRUD, task list)
*Service:* xAgent/ax-1  |  *Depends on:* WP02, WP03
- DELETE /v1/tasks/{id} cooperative cancel per decided semantics (Valkey signal, between-stage poll, in-flight LLM cancel, 202/409/404/503 matrix)
- Per-task timeout: asyncio.timeout around pipeline + terminal status/event; backup sweeper marking orphans AND inserting task.failed outbox row atomically; outbox 7d retention + task_steps 90d retention in the same sweeper
- Contract-9 idempotency on POST /v1/tasks (fail-closed 503 on Valkey outage) — closes Contract-15 cases 12/13
- Auth layer-B: Valkey-cached /v1/authorize verdict (60s TTL, action task:execute) on submission; suspended tenants stop within 60s
- Agent runtime GET/PUT (+status transitions, runtime_version bump, cache invalidation) replacing create-only ON CONFLICT DO NOTHING; agent-config Valkey cache 5min
- GET /v1/tasks list (cursor, filters since/status/agent_id — frontend Task Feed dependency); per-stage step write-through; tracestate propagation + OTel exporter wiring; service README
*Test scope:* Expanded pytest: cancel/timeout/idempotency matrix, sweeper crash-recovery (kill mid-pipeline, assert event emitted), authorize-deny E2E with suspended tenant, runtime update→cache-invalidation→next-task-uses-new-config, list pagination/RLS

### WP09 — RAG minimal service (full phase-05 first-cycle scope)
*Service:* Shared Core/rag (new)  |  *Depends on:* WP06, WP03
- FastAPI skeleton from guardrails/llms template: dual-mode JWT auth, Contract-2 envelopes, livez/readyz/metrics, TenantTx RLS, outbox publisher, Dockerfile
- Schema migrations: knowledge_bases, documents (no bucket-prefix CHECK), chunks, chunk_vectors_1536 + HNSW, kb_acls, outbox, s3_deletions, pricing, tenant_backends (lazy pgvector default) + RLS + grants
- KB CRUD with creation-time alias resolution (immutable resolved fields), default tenant-* ACL row, DELETE KB; query endpoint (two-pass CTE, SET LOCAL ef_search, top_k cap 100, 403 FORBIDDEN_KB)
- Inline ingest (≤100KiB) + fixed/sentence chunking + batched embeddings with deterministic Idempotency-Keys + batched vector INSERTs
- Presigned upload/finalize (env-driven SSE mode for MinIO), Kafka ingestion worker + DLQ + poison-pill, document list/status/delete + s3_deletions sweeper
- Usage metering (units + request_id only) + quota enforcement against Auth limits; platform-skills bootstrap lazy-with-retry incl. ACL row; compose service + migrate mount + topics-init job; IVectorStore interface + PgVectorAdapter
*Test scope:* New pytest suite (≥35 tests): ingest→query E2E with mock embeddings, ACL deny + RLS tenant isolation, worker retry/DLQ, dedup on finalize, quota 413/429, cold-start readiness with LLMs down (lazy bootstrap)

### WP10 — Memory minimal service (full phase-06 first-cycle scope)
*Service:* Shared Core/memory (new)  |  *Depends on:* WP06, WP03
- Skeleton + schema: tenant_config (user_scope_visibility, dedup threshold), memories with principal columns, memory_vectors_1536 + HNSW, sessions keyed by principal_type/principal_id, gdpr_wipe_log, outbox, pricing
- Store: Idempotency-Key short-circuit BEFORE embed, corrected scope-ownership rules (principal_only default), 16KiB cap, mem-embed deterministic keys, dedup ≥0.95 bump-only
- Retrieve: two-pass CTE, top_k cap 50, type/tag filters, user_scope_visibility enforcement, inline last_accessed_at
- By-ID GET/PUT/DELETE (404 anti-existence-leak, immutable-field rejection); sessions endpoint (idempotent, 409 cross-principal); GDPR bulk wipe (either-auth-mode, single txn: wipe_log + DELETE + outbox event); batched TTL sweep
- Usage metering + quota enforcement (Auth limits); compose service + migrate mount
*Test scope:* New pytest suite: scope-visibility matrix (the cross-end-user leak regression test), dedup bump semantics, idempotent store without double-embed, GDPR wipe atomicity, session principal rules, RLS

### WP11 — Tools minimal: registry + tool-web-search
*Service:* Tools/ (two new repos) + Auth seeds  |  *Depends on:* WP03
- Tool Registry service: schemas with corrected split RLS + WITH CHECK (incl. own policy on tool_capabilities), discovery API (UNION, version pinning, tenant priority), platform seed for tool-web-search, eager manifest poll + 30s health poll with ETag + degraded/offline transitions, version retention max 3
- tool-web-search MCP server: GET /manifest (Contract 4), POST /mcp/v1/invoke (dual scope check, input_schema validation 422 with JSON Pointer, 10MiB output cap), SEARCH_PROVIDER env (serpapi|brave|mock for local), per-tenant rate limit fail-open, Idempotency-Key honored, livez/readyz/metrics
- Auth: tool:invoke + tool:tool-web-search:invoke scope seeds, service_acl edges (xagent→registry/tool, registry→auth/tools, tool→auth), bootstrap secrets + compose SPRING_APPLICATION_JSON extension
- Metering ownership per decided fix: xAgent emits tools.invocation.metered from xagent.outbox (lands in WP12); compose services + healthchecks
*Test scope:* Registry discovery/pinning/health-transition tests; invoke contract tests with mock provider (scope deny, schema 422, output cap); cross-tenant RLS mutation-attempt tests proving the marketplace hole is closed

### WP12 — xAgent completion II: enhanced stages (RAG/Memory/Tools) + async/SSE modes
*Service:* xAgent/ax-1  |  *Depends on:* WP08, WP09, WP10, WP11, WP05
- RAG_QUERY stage + rag client: path-param query per amended contract, allowed_kb_ids iteration, top_k≤20/min_score from agent config, 403 pass-through, rag_chunks_returned, PROMPT_BUILD context splice
- MEMORY_RETRIEVE/MEMORY_WRITE stages + memory client (direct POST /v1/memories per decided fix); session_id plumb: Contract-3 input field + tasks column + idempotent session registration; memory_scope='session' becomes functional
- TOOL_LOOP + MCP client: registry client with 5-min ETag manifest cache, allowed_tools version-pin enforcement, per-(endpoint,agent) circuit breaker, retry on conn/5xx never 4xx, Idempotency-Key from (task_id, tool_call_id), max_iterations=10 + tool_loop_limit audit row, multi-call BUDGET_EXCEEDED, tools.invocation.metered via xagent.outbox
- Prompt-context budget (≤30% of token_budget_per_task, RAG→memory→skills truncation order, context_truncated step); cost_budget_per_task column + enforcement; task_steps step_type enum extension
- mode=async (202 + polling, idempotency_key required, sweeper-backed crash recovery); SSE streaming (GET /v1/tasks/{id}/stream, Valkey pub/sub, consuming WP05's fixed llms streaming; content_filter terminal)
*Test scope:* Stage-level unit tests + full-pipeline integration with all services live in compose; Contract-15 full matrix incl. 11-14; budget/iteration caps; async crash-recovery; SSE event-shape golden tests; fail-closed guardrails preserved through all new stages

### WP13 — Production frontend: BFF + SPA
*Service:* frontend/ (new app + bff packages; demo retired)  |  *Depends on:* WP04, WP05, WP07, WP08, WP09
- Next.js + TS SPA scaffold: app shell, routing, design system, 100% env-driven config, Dockerfile + compose service
- BFF: Valkey sessions (encrypted at rest), httpOnly cookie + double-submit CSRF, security headers, downstream injection of Authorization + X-Tenant-ID + X-Request-ID/traceparent, livez/readyz/metrics, 30s dashboard cache, csrf_violations_total
- Login: tenant_id + admin API-key strategy via the built token exchange (px0 SSO slots in later behind the same /bff/me contract)
- Screens: agent list/detail + Agent Builder (create + edit via WP08 runtime PUT, model dropdown from GET /v1/models, full memory_scope enum incl. session, two-step publish with step-2 retry), API key mgmt (raw-key-once modal), task runner (metadata.test=true, 422 blocked-banner, actual-cost display) + task detail timeline + Task Feed (5s long-poll on WP08 list), audit-log viewer + chain-verify button, guardrails dashboard (policy list + WP07 editor + violations log), LLMs usage/cost dashboard (WP05 APIs, cache-token breakdown), RAG KB admin (list/status/test-query via WP09), tenant admin, platform health page
- GitLab CI pipeline; compose deployment mode (no AWS/Kong)
*Test scope:* BFF unit (session lifecycle, CSRF, header injection, key custody); Playwright E2E for core flows (login→create agent→issue key→run task→view timeline/violations/usage) against the compose stack; security-header assertions

### WP14 — E2E integration, observability & production-hardening proof
*Service:* infra/compose + all services  |  *Depends on:* WP12, WP13
- Compose finalization: rag/memory/registry/tool-web-search/frontend-bff/webhook-worker services, migrate mounts, topics-init one-shot job, scheduler container for cron-class jobs, .env.example completion
- Local observability profile: OTLP collector + Tempo + Loki + Grafana + Prometheus with the WP07 SLO alert rules and xAgent timeline/cost dashboards — unblocks Contract-15 cases 6/8/9
- TLS/edge proxy (Caddy or nginx) as the Kong substitute, owning X-RateLimit header passthrough and edge-401 semantics (case 5)
- Runbooks: Neon backup/PITR, Valkey persistence/auth hardening, .env secret handling, signing-key + redaction-key rotation rehearsal records
- Proof: full Contract-15 matrix (1-15, incl. previously unproven 4/6/8/9/11-14) automated in CI; 2x cold full-stack bring-up; basic chaos (kill pod mid-task → sweeper recovers + event emitted; Kafka outage → outboxes drain; Valkey outage → documented fail-open/closed semantics hold); load smoke vs guardrails SLOs and Neon connection budget; final re-audit of code vs amended phase docs
*Test scope:* Full-stack E2E suite in CI (compose, Neon, mock providers), Contract-15 automated smoke, chaos scripts, load smoke; exit = 2x cold green + all gating cases pass

## Explicitly excluded (later phases per the plans themselves)

- Auth Phase 10/13: cnf token binding (mTLS/DPoP), SPIFFE attestation + agent_workload_acl, OPA bundle compilation, behavior-policy alert middleware (P10) and blocking/quarantine enforcement (P13), audit_log monthly partitioning, tenant hard-delete 30d job, onboarding reputation feeds + manual-review UI
- Auth Phase 9C/11: Component 10 approval flow + step-up (approval tokens, tenant_step_up_policy), px0 verification machinery + upstream-identity seed + X-Px0-User-Token paths, px0 user-revocation propagation, billing-emitter integration, policy management API + ABAC conditions + A2A delegation tokens/chains (Phase 10)
- LLMs enterprise (own 📋): routing hints + provider failover (Component 7), Gemini/Groq/Azure/Mistral/Ollama/Bedrock adaptors, semantic response cache (Component 9), budgets + hard-stop 402 (Component 10), per-agent rate limits, predictive token pre-limiting, PII log masking, interceptor pipeline, provider health monitoring, prompt-caching exposure, llms-billing service extraction, KEDA scaling
- Guardrails own 📋 + P13: async check mode, audit enforcement mode, NER rules (pii-ssn/passport/name), semantic topic-blocklist, output-hallucination LLM judge, output-format, window/post streaming modes, Llama Guard 2 split pod, embedded-library mode, hot policy reload, trend/stats API beyond the violations list, watermarking, policy inheritance, platform HMAC-key rotation runbook, 2000 checks/sec load targets, violations archival
- xAgent 9B/9C + enterprise: A2A receiver/sender + agent_registry + delegation tokens + loop detection, orchestrator + workflows/DAG + human-in-the-loop, guardrails fail-open circuit breaker (needs Phase-4 non-block designation), cypherx.auth.agent.registered stub-row consumer
- RAG enterprise: hybrid/BM25 search, re-ranking, query expansion, HTML/JSON/CSV/URL/S3/webhook sources, semantic/recursive/code chunking, document versioning + re-ingestion, multimodal OCR/image embeddings, Pinecone/Qdrant adapters, is_public_read mixed-scope RLS
- Memory enterprise: auto-extraction (/extract), summarisation, working memory (Valkey), importance decay, consolidation job, re-embed admin job, user_scope_acl table, async last-accessed tracker, embedding circuit breaker
- Tools enterprise: external publisher submission flow (Trivy/Snyk scan, sandbox lint, pending_review), marketplace publish/install + GET /v1/public/tools, all other tool servers (code-exec/gVisor, http-client, file-ops, email, calendar, browser, image-gen, pdf-gen, data-analysis, notify), Anthropic-MCP bridge
- Skills (Phase 8) entirely — not required by any first-cycle service; xAgent SKILL_LOAD slot stays disabled
- Frontend enterprise: px0 SSO + team management + billing iframe (Phase 11), workflow canvas (Phase 10), Memory dashboard + cross-tenant admin-read pattern, multiplexed tenant-wide SSE feed, per-service mini-UIs/@cypherx/admin-ui, white-label theming, frontend_mode toggle, Skill/Tool catalog UIs (Phases 7/8)
- Deploy-target machinery (infra phase): K8s/ArgoCD/Helm, Kong, Istio AuthorizationPolicies, Argo Rollouts canary, PodDisruptionBudgets, HPA/KEDA, egress NetworkPolicies, SPIRE, Terraform/Terragrunt + S3/CloudFront/ACM/Route53, Doppler operator, GitHub-Actions pipelines — compose-parity equivalents land in WP14 instead

## Execution risks

- Neon (remote pooled Postgres, TLS) latency + connection limits vs the guardrails 30/50ms SLOs and the new per-request Valkey/DB work across all services — mitigated by mandatory policy/plan caches and post-response persistence, but the SLO must be re-validated under load in WP14 and may force budget revision
- The llm_call_id billing-key migration (WP02) touches the proven, revenue-bearing usage/outbox path of the live spine — needs a careful migration with replay-journal compatibility and a regression proof that Kafka/Postgres stay consistent
- detoxify/torch packaging: ~2GB ml image, baked checkpoint supply chain, eager-load startup time — risk of CI/registry pain and slow cold starts; the slim/ml split must be enforced or prod silently runs the keyword stub again
- Streaming correctness rework (tool-call aggregation rewrite in both providers) plus xAgent SSE-through-Valkey is the most regression-prone area of WP05/WP12; the existing text-streaming behavior is proven and must be golden-tested before refactor
- Shared Valkey becomes load-bearing for fail-CLOSED semantics (xAgent idempotency 503, guardrails rate limiting) — a Valkey outage now degrades writes platform-wide; persistence/auth hardening and capacity sizing (idempotency bodies + caches + pub/sub) land only in WP14, late
- Scope breadth: three brand-new services (RAG/Memory/Tools) + a production frontend in one cycle, with WP06 (embeddings/idempotency) a single-threaded critical path that gates WP09/WP10/WP12 — slippage there cascades; pgvector HNSW build times/memory on Neon are unproven
- Auth (Kotlin/Spring) absorbs the most cross-cutting change (quotas, rate limits, revocation on every request path, outbox, onboarding, webhooks) — performance regressions or fail-open/closed mistakes here affect all services; WP03/WP04 need the strictest review gates
- Plan-doc amendments (WP01) require user approval per the dev process before any code fix; if contested decisions (e.g. BYOK sealed-in-Postgres, plan-tier ownership in Auth, MEMORY_WRITE over HTTP) reopen late, downstream packages re-plan
- Onboarding email/captcha are pluggable but the production providers (SES/SMTP account, Turnstile keys) don't exist yet — the prod path ships tested only against mocks until credentials are provisioned
- No K8s target exists: several ⚡ items are deliverable only as compose-parity equivalents; 'done' must be formally redefined (WP01) or audits will keep flagging deploy-target items forever

## Bottom line

The proven four-service spine is solid, but the path to '100% of the phase plans' runs through 26 plan defects first — 9 of them blockers (jti replay window, AWS-only BYOK, the request_id billing-drop, guardrails' policy-CRUD inversion, xAgent's Contract-15/idempotency hole and live agent-id mis-attribution bug, the px0-only frontend login, the MEMORY_WRITE topic void, and RAG/Memory's dependency on absent LLMs surfaces) — all with decisive fixes amended into the docs in WP01. Development then proceeds as 14 dependency-ordered packages: cross-service foundations (Valkey everywhere, DB-driven config de-hardcoding, auth outbox, billing-key fix, live-bug fixes), Auth completion in two waves (quotas/limits/revocation enforcement first, since llms/guardrails/rag/memory rate-limiting all consume the new limits endpoint; then onboarding/webhooks/audit pipeline), LLMs in two waves (hardening + streaming correctness, then embeddings/BYOK — the single critical path gating everything RAG/Memory), guardrails completion, xAgent reliability layer, the three minimal services (RAG, Memory, Tools registry + web-search), xAgent's enhanced stages + async/SSE, the production frontend on a Valkey-sessioned BFF with tenant+API-key login, and a final E2E/observability/hardening package that proves the full Contract-15 matrix 2x cold. Phase-13+/9B/9C/Skills items are explicitly excluded so '100%' stays auditable; the top execution risks are Neon latency vs the guardrails SLOs, the billing-key migration on the live spine, detoxify packaging, and the WP06 critical path.