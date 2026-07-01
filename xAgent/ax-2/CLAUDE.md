# CLAUDE.md — xAgent/ax-2

> **EMPTY REPO (placeholder).** Reserved for the CypherX xAgent **A2A & Orchestration** tier — the **a2a-router** + **Orchestrator** (Phase 10 / xAgent sub-phases 9B + 9C). Sibling `xAgent/ax-1` is the already-built Phase-9A single-agent runtime. Platform root guide: `../../CLAUDE.md`.

## What this is
This repo currently contains **only `.git`** — no commits, no source, no manifests, no Dockerfile. HEAD is on `feature/base-implementation`; remote is `gitlab.com/cypherx-ai/xagent/ax-2.git`. It is the intended home of the **xAgent A2A & Orchestration** component described in `archive/Manoj/phases/phase-10-a2a-orchestration.md` (which expands xAgent sub-phases **9B** = agent-to-agent communication and **9C** = orchestration engine). **Implementation status: empty.** This is expected: Phase 10 is entirely 📋 (not first cycle), gated on Phase 9A passing the Contract-15 smoke test twice + 7 consecutive clean days in dev/staging before kickoff.

Two logical components live here (per spec they may ship as one or two services — undecided):
- **a2a-router** — fixed endpoint `http://a2a-router.xagent…:8080/v1/a2a/tasks`; verifies chain-aware A2A JWTs, looks up `receiver_agent_id` in `xagent.agents`, picks an `agent-runtime` pod by consistent-hash on `agent_id` (informer over K8s EndpointSlices), and forwards to that pod's internal execute endpoint. Supports A2A `sync` / `async` / `stream` modes, idempotency, and cancel.
- **Orchestrator** — production-grade workflow DAG execution (sequential / parallel / conditional / loop / human-in-the-loop), `/v1/workflows…` endpoints, state in `xagent.workflows` + `xagent.workflow_tasks`.

## Tech stack
**Nothing committed yet.** No language is locked in by code on disk. By platform convention (sibling `ax-1` and the other SharedCore services) the expected stack is **Python 3.12 · FastAPI · `uv` · psycopg3 (async, RLS) · structlog · Prometheus**, with Valkey + Redpanda (Kafka) as runtime deps and Postgres EXTERNAL (Neon). Do not assume this until the first commit lands — confirm against actual manifests when they appear.

## Repository layout
```
xAgent/ax-2/
└── .git/        ← only this exists (no commits, branch feature/base-implementation)
```
There is no `src/`, `tests/`, `db/`, `pyproject.toml`, `Dockerfile`, or `README.md` yet. For the reference shape of a built xAgent service, read the sibling `../ax-1/` (FastAPI app `agent_runtime`, `src/`, `db/migrations/`, `pyproject.toml` + `uv.lock`, `Dockerfile`).

## Build, test, run
**None — the repo is empty.** No build/test/run commands, no Dockerfile, no compose service.
- ax-2 is **NOT** in `infra/compose/docker-compose.yml`. The compose `xagent` service builds from `xAgent/ax-1` only (host map `8083->8080`); there is no `a2a-router` or `orchestrator` container wired in at this phase.
- Intended deploy shape (phase-10 checklist, for when it is built): `a2a-router` as a K8s Deployment, **3 fixed replicas** (no HPA — ring stability), startup probe with ~120s grace for informer sync, **in-container port 8080** (canonical), `readyz` gating on **Postgres + Auth + ≥1 `agent-runtime` endpoint**, `livez` process-only, `/metrics` Prometheus. Health/port conventions follow Contract 7.

## Configuration & secrets
**No env vars are read yet (no code).** When built it will follow the platform pattern: prefix-less env from **Doppler** (only `.env.example` is committed, never a real `.env`); Postgres DSN from env (Neon, external). Expected vars by analogy to `ax-1` + phase-10: `DATABASE_URL` (RLS-scoped `xagent_user`, not superuser; READ-ONLY for cancel-auth), `VALKEY_URL`, `KAFKA_BROKERS` (+ `KAFKA_SASL_PASSWORD`), `AUTH_JWKS_URL` / `AUTH_SERVICE_URL`, `SERVICE_BOOTSTRAP_SECRET` (Contract 12 — mint own service JWT), `AGENT_RUNTIME_SERVICE` (Service whose EndpointSlices the informer watches), `MAX_DELEGATION_DEPTH_DEFAULT` ("5"), `CANCEL_TOPIC` ("cypherx.agent.task.cancel.requested"), plus orchestrator/router knobs (idempotency TTL, fanout threshold). Local default is keyless (`MOCK_PROVIDERS=true` etc.). None of this is wired yet.

## Contracts & cross-repo dependencies
`contracts/` is the single source of truth; this service will be a consumer, never an author.
- **Consumes contracts:** `contracts/a2a/` (`delegation.schema.json`, `task-request.schema.json`, `task-response.schema.json`, `task-types.md`) and `contracts/workflows/dag.schema.json` (📋 — not yet created), plus platform-wide Contract 1 (JWT), 3 (task/A2A envelope + 256 KiB caps), 7 (health), 8 (trace/request-id), 9 (idempotency), 12 (service JWT), 13 (tenant/RLS), 16 (step-up approval token). Note the a2a schemas are `x-enforcement-phase: phase-10` — defined now, enforced here.
- **Calls / called by:** receives A2A delegations from agent-runtime (`ax-1`) agents and from the Orchestrator; forwards to `agent-runtime` pods (`/v1/internal/a2a/execute`); calls **Auth** (JWKS verify, service-token mint) and reads `xagent.agents`. Service-edge ACL rows (Phase 10 migration): `orchestrator → a2a-router`, `a2a-router → auth-service`, `a2a-router → agent-runtime`.
- **Kafka produced:** `cypherx.agent.a2a.delegated`, `cypherx.agent.workflow.completed`, `cypherx.agent.workflow.failed`, `cypherx.agent.workflow.approval.recorded` (also `cypherx.agent.a2a.cycle_suspected` on fanout breach; **consumes/publishes** `cypherx.agent.task.cancel.requested`, 12 partitions + DLQ). All carry `request_id` + `trace_id`.
- **DB:** owns **no new schema of its own** — reuses the **`xagent`** Postgres schema (`xagent.workflows`, `xagent.workflow_tasks`, plus A2A columns on `xagent.tasks`: `delegation_root_agent_id`, `a2a_callback_url`, `a2a_callback_secret`). DDL is a **cross-phase migration** under `platform-migrations/phase-10/` (4 files; xagent.tasks columns, xagent.workflows+workflow_tasks, auth.tenants+auth.callback_allowlist, auth.service_acl seed), NOT in this repo (three-team CODEOWNERS gate: Auth + xAgent + Platform).

## Invariants & guards (do NOT break)
These come from phase-10 and apply once code exists here (capture them now so they aren't reinvented):
- **Fixed router endpoint, no per-agent Services.** Every A2A call goes to the one `a2a-router` URL; `receiver_agent_id` travels in the **body envelope**, never in the URL/path. Building per-agent K8s Services is an explicit anti-pattern. Consistent-hash ring needs an **explicit EndpointSlices informer** — plain K8s Service DNS gives round-robin, not hashing.
- **Authority lives in the JWT, never the body.** A2A JWTs are chain-aware; receivers **verify** the delegation chain (sig continuity per entry, monotone scope subset, depth ≤ tenant max (default 5), cycle check, root-expiry inheritance) — they **never construct** chains (Auth `/v1/agents/{id}/a2a-token` issues them).
- **A2A is NOT governed by `service_acl`** — agent-to-agent edges are authorized by the delegation chain, not ACL rows. Only the 3 service edges above use ACL.
- **Idempotency mandatory** for all three modes (`Idempotency-Key`, Valkey `a2a-idemp:{tenant}:{receiver}:{key}`, 24h TTL; required for `mode=async`); replay returns cached body + `Idempotent-Replay: true`; in-flight → 409; fail-open on Valkey outage.
- **Async callback security:** SSRF-validate `callback_url` (HTTPS only, no RFC1918/loopback/link-local/metadata, tenant allow-list `auth.callback_allowlist`); per-task 32-byte HMAC secret in `xagent.tasks`, returned at 202, **zeroized on terminal status**, never logged/returned. Async polling uses service-JWT + agent-JWT (NOT the 5-min A2A token).
- **RLS / tenant isolation:** runtime role is not superuser; every query sets `app.tenant_id`; cross-tenant rows surface as 404, never leaking existence. Cross-tenant delegation forbidden by default.
- **256 KiB caps** on A2A input/output and workflow `subtask_dag`/`output` (S3-reference pattern for larger; `cypherx-a2a-output-<env>` bucket, SSE-KMS, 24h lifecycle). Workflow approval requires a Contract-16 step-up token (one-shot, resource-scoped `workflow:<id>` exact match, ≤15 min, jti replay-checked) AND `workflow:approve` scope.
- **DAG cycle validation (Kahn's algorithm) is mandatory BEFORE execution** (LLM decomposition occasionally emits cycles) → workflow `failed`, `error_code='INVALID_DAG'`, no subtasks spawned.
- **Cancel propagation is Kafka fan-out with PER-POD consumer groups** (each pod must see every cancel); cancel is idempotent (terminal task → 200 no-op); A2A cancel-auth gates to root agent (`chain[0].from`) or `platform:admin`.
- Optimistic locking (`version` column) on `workflow_tasks` fan-in nodes; workflow state + Kafka events emitted atomically via the **transactional outbox** (reuse `xagent.outbox`).

## Gotchas & current status
- **The repo is empty by design** — do not treat this as broken or half-deleted. There is genuinely nothing to build/run until Phase 10 starts.
- **Do not confuse with `ax-1`.** `ax-1` = Phase-9A single-agent `agent-runtime` (built, in compose as `xagent`). `ax-2` = Phase-10 a2a-router + Orchestrator (this repo, empty). Phase 9A intentionally only "lays the rails" (a flagged stage pipeline) so A2A/orchestration land here later without re-architecting.
- **Service split undecided:** phase-10 treats a2a-router and orchestrator as distinct K8s deployments; whether they live as one or two services/repos is a planning decision (each sub-phase requires its own service-architecture plan first).
- **No separate agent registry, no heartbeats:** discovery is a read-only view over `xagent.agents` (GIN index on `capabilities`); availability = `auth.agents.status=active` AND `xagent.agents.status=active` AND ≥1 Ready agent-runtime pod (via EndpointSlices).
- **Post-design audit (2026-05-25) flagged real risks** to address when building: router as control-plane bottleneck (peer-delegation toggle), cancel fan-out scaling (re-key cancels by tenant), workflow_tasks lock contention (aggregate table for >50-fanin), "Temporal-lite" maintenance burden, chain-validation latency (Valkey chain cache), hash hotspots, approval-window operational load.
- **Nothing is verified against running code** because there is none — every design fact above is sourced from `archive/Manoj/phases/phase-10-a2a-orchestration.md` and `contracts/a2a/`. Re-read those before the first implementation commit.
