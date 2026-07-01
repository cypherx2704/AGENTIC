# Phase 10 — A2A & Orchestration (Standalone Expansion)
> **Status:** ⏳ Pending | **Depends On:** Phase 9 (9A complete) | **Blocks:** Phase 12
> **First Cycle:** 📋 Not required for first cycle. Implement after Phase 9A is stable.

---

## Phase Overview

Phase 10 expands on the A2A and Orchestration sub-phases started in Phase 9 (9B and 9C). While those sub-phases define the design and basic implementation, Phase 10 focuses on making multi-agent communication and orchestration **production-grade**: reliable, observable, fault-tolerant, and scalable.

**Deliverable:** Production-ready A2A protocol (sync + async + streaming modes), a robust Orchestrator with DAG execution, and a full workflow management system.

> 🏗️ **Service Architecture Note:** The A2A router, orchestrator, and agent registry service architectures must be planned separately before implementation of each component.

---

## High Level Design

### Multi-Agent Communication Architecture

```
Orchestrator Agent
  │  (receives goal, decomposes, routes)
  │
  ├──[A2A sync]──────► Research Agent     → returns result
  │
  ├──[A2A async]─────► Writer Agent       → callback when done
  │
  └──[A2A stream]────► Analysis Agent     → SSE stream of partial results

A2A Router (ns: xagent)
  └── Routes incoming A2A tasks to the correct agent instance
      based on agent_id → K8s service → pod
```

### A2A Protocol Modes

| Mode | When to use | How it works |
|------|-------------|--------------|
| `sync` | Fast tasks (<30s) | POST → wait → response in body |
| `async` | Long tasks (>30s) | POST → 202 Accepted → poll GET or callback |
| `stream` | Partial result delivery | POST → SSE stream of partial outputs |

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> Items here expand on Phase 9B/9C designs. Design fully, implement after Phase 9A verified.

---

### Component 0 — A2A Routing Model (How a request reaches the right agent)

This is the most commonly missed detail. The agent-runtime service runs many *agent definitions* but is one K8s Deployment with many pods. There is no per-agent K8s Service. So how does `POST http://<receiver-agent.xagent>/v1/a2a/tasks` reach the correct logic?

**Decision: agent-runtime is multi-tenant in-process; a2a-router does name → pod routing only.**

```
1. Agent definitions are data, not pods. All agent-runtime pods can serve any agent in their tenant.
2. A2A endpoint is FIXED for every receiver:
     http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks
   There is NO per-agent Service URL. Per-agent Services don't scale (hundreds/thousands
   of agents per tenant), and they conflict with the multi-tenant pod model below.
3. The A2A message envelope carries receiver_agent_id (Contract 3).
4. a2a-router:
   a. Verifies the A2A JWT locally (chain-walk per Component 2).
   b. Looks up receiver_agent_id in xagent.agents (NOT a separate registry — see Component 3)
      to confirm tenant + capabilities.
   c. Picks an agent-runtime pod (consistent hash on agent_id for cache locality, with fallback).
   d. Forwards the request to that pod's /v1/internal/a2a/execute endpoint.
5. agent-runtime pod loads the agent definition (Valkey cache) and runs the execution pipeline.

Endpoint discovery for the consistent-hash ring (implementation detail that matters):
  - a2a-router watches Kubernetes EndpointSlices for the agent-runtime Service via an
    in-process client-go informer (or equivalent).
  - Hash ring is rebuilt on endpoint change. Fallback: when the hashed-to pod is unhealthy
    (failed readiness OR endpoint missing), the router picks a random healthy pod from the
    ring and continues.
  - Implementers default to "K8s Service DNS does it for me" — that gives random round-robin,
    NOT consistent hashing. Explicit informer is required.

Why consistent hash on agent_id:
  - Keeps the agent's hot context (memory, skill cache) on one pod for cache locality.
  - On pod removal, traffic re-hashes; cold-start is acceptable for occasional events.

Why a router (not direct service-per-agent):
  - With hundreds/thousands of agents per tenant, creating one K8s Service per agent doesn't scale.
  - The router is a 100-line Envoy-config or a Go service; trivial to operate.
```

A2A endpoint URL in the registry is therefore always `a2a-router`, never a per-agent URL. External A2A (cross-organization in future) goes through Kong with the same routing.

---

### Component 1 — A2A Protocol Implementation (Full)

> **All examples below use the fixed a2a-router URL per Component 0.** There is no per-agent Service URL. `receiver_agent_id` lives in the body envelope (Contract 3).

**Body size caps (Contract 3 parity):**
- A2A `input` ≤ 256 KiB; A2A `output` ≤ 256 KiB.
- Larger payloads MUST use the S3-reference pattern: write to `s3://cypherx-a2a-output-<env>/<tenant_id>/<task_id>` with SSE-KMS, return `output.ref` instead of inline body. Bucket lifecycle: objects deleted after 24h. Same convention as tools Phase 7 post-edit.

**Schema additions to `xagent.tasks` (cross-phase migration owned by Phase 10 — see migration ownership block):**

```sql
-- A2A async-mode callback + delegation-root storage. All columns NULL for non-A2A tasks.
ALTER TABLE xagent.tasks
  ADD COLUMN delegation_root_agent_id UUID,         -- chain[0].from at task accept time; used by cancel-auth (Component 5b)
  ADD COLUMN a2a_callback_url         TEXT,         -- SSRF-validated at /v1/a2a/tasks accept; immutable after
  ADD COLUMN a2a_callback_secret      BYTEA;        -- raw 32 random bytes; NOT base64. NULL outside the task's lifetime.

-- Index for cancel-auth lookup (a2a-router queries by task_id; PK suffices, but the
-- delegation-root column is needed in the projected SELECT, not as an index).
COMMENT ON COLUMN xagent.tasks.delegation_root_agent_id IS
  'For A2A-accepted tasks: chain[0].from of the original delegation. Used by a2a-router
   DELETE auth (Component 5b) to gate cancel to the root agent or platform:admin.';

COMMENT ON COLUMN xagent.tasks.a2a_callback_secret IS
  'Raw 32-byte HMAC secret returned to sender at 202 Accepted. Zeroized server-side
   (UPDATE ... SET a2a_callback_secret = NULL) once the task reaches a terminal status
   (completed | failed | cancelled). Never logged, never returned in any GET.';
```

> **Storage rationale (read before alternatives are proposed):**
> - `a2a_callback_secret` lives in Postgres, not Valkey, because async tasks routinely
>   outlive a Valkey eviction or a router restart. The sender holds the matching secret;
>   losing the receiver-side copy means callbacks can't be verified → effectively
>   silent task abandonment.
> - `delegation_root_agent_id` is denormalised from the original delegation chain
>   (which is discarded after JWT validation) because the chain is in a short-lived
>   token, not in any durable row. Storing the root agent at accept time is the only
>   way to authorise a cancel 30 minutes later.
> - All three columns are NULL for non-A2A tasks (sync/direct invocations from the
>   same agent runtime). CI test: cancel via /v1/tasks/{id} (non-A2A path) does not
>   read these columns.

**Sync Mode:**
```
Sender:
  POST http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks
  Headers:
    Authorization:   Bearer <chain-aware A2A JWT>   ← Component 2; carries delegation_chain
    traceparent:     00-<trace-id>-<span-id>-01     ← Contract 8 (also tracestate per Component 6)
    X-Request-ID:    <propagated from sender>       ← Contract 8; never minted if header present
    Idempotency-Key: <uuid>                          ← Contract 9; honored on every mode
  Body: A2A task schema (Contract 3) — receiver_agent_id in body, NOT in URL.
  Timeout: task.timeout_seconds (max 120s for sync; overall Contract 3 cap is 900s for async/stream).

Receiver (via a2a-router):
  1. a2a-router verifies A2A JWT (Component 2 chain-walk).
  2. Idempotency check (Contract 9 — MANDATORY for all three modes — sender retries WILL
     happen on network blips and would double-bill downstream LLM/tool spend):
       Valkey key: a2a-idemp:{tenant_id}:{receiver_agent_id}:{idempotency_key}
       SET NX EX 86400
       Hit (completed)  → return cached response with header `Idempotent-Replay: true`.
                          NO downstream task spawn, NO Kafka publish.
       Hit (in_flight)  → 409 IDEMPOTENT_REQUEST_IN_FLIGHT with `Retry-After: 2`.
       Miss             → SET marker, proceed. Cache completed response after step 4.
       Missing header   → proceed without idempotency (callers SHOULD provide; not all do).
       Valkey outage    → FAIL OPEN with telemetry counter a2a_idempotency_skipped_total{reason},
                          log WARN. The cancel path (Component 5b) is idempotent independently,
                          so a duplicated task can still be stopped.
  3. Forwards to selected agent-runtime pod.
  4. Pod executes task (blocking).
  5. Returns 200 with A2A response schema; a2a-router updates the Valkey entry to
     status=completed with the response body cached (gzip+base64, ≤32 KiB; over-cap
     entries store status only, replay re-executes — acceptable for first cycle).
```

**Async Mode:**
```
Sender:
  POST http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks
  Headers: (same as sync — `Idempotency-Key` MANDATORY here: async retries are most common)
  Body: { ..., "mode": "async", "callback_url": "https://..." }

Receiver:
  1. Validates callback_url against Contract 3 SSRF guards:
       - HTTPS only
       - Host MUST NOT resolve to RFC1918 / loopback / link-local / cloud-metadata IPs
       - Host MUST match the tenant's callback allow-list (configured in Auth)
       Failure → 400 INVALID_CALLBACK_URL with reason.
  2. Generates a per-task HMAC secret (32 random bytes), stores it with the task.
  3. Returns 202 Accepted:
       {
         "task_id":         "<uuid>",
         "callback_secret": "<base64>",            ← sender stores this server-side
         "poll_url":        "http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks/<task_id>"
       }

Sender can poll:
  GET <poll_url>
  Headers:
    Authorization:         Bearer <sender's service-jwt>     ← Phase 7 standard, NOT the
    X-Forwarded-Agent-JWT: <sender's current agent-jwt>      ← short-lived A2A token
    traceparent:           <propagated>
  a2a-router:
    1. Looks up the task by task_id.
    2. Verifies the caller's agent_id matches chain[0].from of the original delegation
       (cross-tenant lookup forbidden by Component 2b — always same tenant).
    3. Returns { status: "running" | "completed" | "failed", output: ... }.
  Why not the A2A JWT for polling: A2A JWTs are 5-min tokens (Contract 12 parity); an
  async task that runs 30 min has a long-dead A2A token by poll time. Standard
  service-JWT + agent-JWT pair is the only auth available.

OR receive callback (HMAC-signed — Contract 3 post-edit requirement):
  Receiver POSTs to callback_url when complete:
    Headers:
      Content-Type:           application/json
      X-CypherX-Signature:    HMAC-SHA256(callback_secret, body) hex
      X-CypherX-Timestamp:    <unix-seconds>          ← replay protection
    Body: A2A response schema (Contract 3 post-edit)
  Sender verifies signature with the callback_secret it stored at 202 Accepted time;
  rejects on signature mismatch or timestamp skew > 5 minutes.

  Per-task secret model (instead of Contract 3's per-agent secret):
    - The per-agent secret model would require the RECEIVER to know the SENDER's
      callback secret — they're in different agents, often different teams. Unworkable.
    - Per-task secret is generated by the receiver at task accept and returned in 202.
      Lives in receiver's xagent.tasks row, expires when the task does.
    - Contract 3 post-edit allows per-task secrets for A2A flows alongside the per-agent
      secret for non-A2A callbacks.
```

**Stream Mode:**
```
Sender:
  POST http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks
  Headers: (same as sync)
  Body: { ..., "mode": "stream" }
  Connection stays open (SSE)
  Events: same format as Task SSE (Component 5 in Phase 9)
```

---

> **Service ACL does NOT govern A2A calls.** `auth.service_acl` (Phase 2 / Phase 7 / Phase 8 post-edits) gates *service-to-service* edges (xagent→llms-gateway, xagent→tool-registry, etc.). A2A calls are *agent-to-agent* and are authorized by the delegation chain (Component 2), not by ACL rows. Implementers following the cross-service-ACL pattern from earlier phases will create thousands of useless ACL rows for agent pairs. Don't. The `service_acl` continues to govern the *service edges* only: `orchestrator → a2a-router`, `a2a-router → auth-service`, `a2a-router → agent-runtime`.

---

### Component 2 — A2A JWT Validation (Chain-Aware)

A2A tokens are NOT plain agent JWTs. They carry the full delegation chain (Phase 2 Component 8d) so receivers can verify the entire path from root agent to themselves without calling back to Auth on the hot path.

```
Every A2A request validated:
  1. Bearer JWT in Authorization header — RS256, JWKS-verified (Contract 1).
  2. JWT claim sub == requesting agent_id (agent-to-agent, never user).
  3. JWT claim aud includes receiver agent_id (or "a2a-router" for inbound proxy).
  4. JWT must carry delegation_chain[] (per Phase 2 Component 8d).
  5. Chain walk (every entry):
       a. entry.sig verifies against JWKS[entry.kid].
       b. entry.to == next entry's from (continuity).
       c. entry.expires_at ≤ delegation_root_expiry.
       d. entry.scopes ⊆ previous entry.scopes (monotone non-increasing).
  6. chain[-1].to == this agent's agent_id.
  7. NOW < delegation_root_expiry.
  8. Requested action ⊆ chain[-1].scopes.
  9. Cycle check: receiver agent_id MUST NOT appear earlier in chain → 401 DELEGATION_CYCLE.

On any failure: 401 DELEGATION_CHAIN_INVALID with a `reason` field naming the failed step.
On success: extract chain[0].from (root agent) for audit, chain[-1].scopes for enforcement.
```

> All chain construction rules (depth limit, scope subset, transitive flag, root_expiry inheritance) are enforced at the **issuance** side by Auth /v1/agents/{id}/a2a-token (Phase 2 Component 8d). Receivers only verify; they never construct.

---

### Component 2b — Delegation Chain Semantics (Operational)

This is the operational complement to Phase 2 Component 8d. Auth enforces *what is mintable*; this section defines *how the chain behaves under realistic A2A topologies*.

**Depth budget per workflow:**

```
Default MAX_DELEGATION_DEPTH = 5 (configured per tenant in auth.tenants.delegation_max_depth).

Why 5: covers most realistic orchestration patterns
  - Orchestrator → Specialist → Sub-tool-agent → Reviewer = 4 hops
  - Plus a buffer for one unexpected re-delegation.
Patterns needing >5 are flagged as design smells and require explicit tenant config bump.

The orchestrator agent itself counts as the root (depth 0). Each A2A delegation adds 1.
```

**Expiry inheritance — explicit rules:**

| Scenario | Rule |
|----------|------|
| Root token TTL = 5 min | All chain links capped at 5 min from root issuance |
| B re-delegates at minute 3 with 5 min requested | Auth caps at 2 min remaining (root expires at minute 5) |
| Root has no explicit expiry | Auth treats `delegation_root_expiry = root.exp` (the JWT's own exp, max 1h per Contract 1) |
| Workflow exceeds root expiry | All in-flight A2A calls fail next-hop validation → cancellation propagates per Component 5b |

**Transitive delegation matrix:**

```
                        chain[-1].transitive
                        false                true
chain[0].from == B  →   ALLOWED (root)      ALLOWED
chain[0].from != B  →   REJECTED            ALLOWED

Default at root issuance: transitive=false. To enable, root agent's owner must hold
the delegation:transitive scope (gated by RBAC, added to default platform policy as
denied-by-default).
```

In practice this means: an Orchestrator agent (which legitimately fans out to many specialists who then fan out further) must be configured with `delegation:transitive`. A leaf specialist that should not re-delegate is left without it — even if it tries to mint a child token, Auth rejects.

**Cycle detection (two layers):**

```
Layer 1 (synchronous, in-chain — Phase 2 Component 8d step 5):
  - Catches A → B → C → A within a single chain.
  - O(depth) string comparison at /v1/agents/{id}/a2a-token issuance.

Layer 2 (asynchronous, cross-chain via Kafka):
  - Catches indirect cycles: A → B publishes event → triggers a NEW root task that
    eventually calls A again. The new root has a fresh chain, so Layer 1 cannot see it.
  - a2a-router maintains Valkey set a2a-fanout:{delegation_root_task} of distinct
    agent_ids reached. INCR + check cardinality on every A2A invoke.
  - Threshold: 50 distinct agents per root_task (configurable per tenant).
  - On breach: publish cypherx.agent.a2a.cycle_suspected, quarantine the root agent
    via Component 5c, cancel the workflow.

  Why 50: gives orchestrators ample headroom; catches runaway recursion long before
  cost-explosion territory.
```

**Cross-tenant delegation — forbidden by default:**

```
chain[i].from and chain[i].to MUST be in the same tenant_id (extracted from each
agent's auth.agents row at chain-mint time).

Cross-tenant A2A (future federated agents) requires:
  - explicit auth.cross_tenant_trust row (caller_tenant, target_tenant, allowed_scopes)
  - SPIFFE federation between trust domains (Phase 2 Component 8c)
  - Approval token (Phase 2 Component 10) at the cross-tenant hop
  - delegation_chain entry carries `cross_tenant: true` marker and trust_bundle_id

Out of scope for first enterprise rollout; mentioned here so the chain schema
reserves room for it without a future breaking change.
```

**Audit trail:**

```
Every Kafka event cypherx.agent.a2a.delegated MUST include:
  - delegation_root_task (workflow root)
  - delegation_depth at this hop
  - chain (full chain at this hop; bounded ≤5 entries by depth limit)

Compliance pipeline can reconstruct any workflow's full delegation tree from these
events alone — no need to fish individual JWTs out of logs.
```

---

### Component 3 — Agent Discovery (Read-Only View over xagent.agents)

> **No separate `xagent.agent_registry` table.** The prior draft introduced one, duplicating fields already in `xagent.agents` (Phase 9) and adding a useless `a2a_endpoint` column (always the same a2a-router URL per Component 0). Two sources of truth = guaranteed drift. Discovery is a thin read-only API over `xagent.agents`.

**No heartbeat mechanism.** Per-agent heartbeats are a leftover from the per-agent-pod model that Component 0 explicitly replaced. Agent definitions are data; "availability" means:
- `auth.agents.status = 'active'` AND
- `xagent.agents.status = 'active'` AND
- at least one `agent-runtime` pod is `Ready` (checked via K8s EndpointSlices, same source the consistent-hash ring uses in Component 0).

```
Required schema additions to xagent.agents (Phase 9 schema) for capability search:
  capabilities  JSONB  NOT NULL DEFAULT '[]'   ← already present in Phase 9
  + GIN index for fast filtering:
    CREATE INDEX idx_xagent_agents_capabilities_gin
      ON xagent.agents USING gin (capabilities jsonb_path_ops);

Discovery API (read-only, RLS-isolated per Contract 13):
  GET  /v1/registry/agents
       List all agents in caller's tenant (paginated).
  GET  /v1/registry/agents?capability=research
       Filter by capability (JSONB containment: WHERE capabilities @> '[{"type":"research"}]').
  GET  /v1/registry/agents/{id}
       Get a single agent's details (404 if cross-tenant — RLS gates).

Underlying query example:
  BEGIN;
  SET LOCAL app.tenant_id = $tenant_id;
  SELECT agent_id, name, capabilities, status
    FROM xagent.agents
   WHERE status = 'active'
     AND capabilities @> $expected_capability  -- e.g. '[{"type":"research"}]'
   LIMIT $limit;
  COMMIT;

Caching:
  Valkey key: agent-discovery:{tenant_id}:{capability_hash}   TTL = 30s
  Invalidation on xagent.agents update via cypherx.agent.runtime.updated event (📋 emit).
```

> **Endpoint URL is implicit.** Discovery does NOT return a per-agent endpoint URL — every A2A call goes to `http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks` with `receiver_agent_id` in the body envelope (Component 0). Returning a per-agent URL would mislead implementers into building per-agent Services.

---

### Component 4 — Orchestrator (Production Grade)

**DAG Execution Engine:**
```
Workflow DAG:
  Nodes: subtasks
  Edges: dependencies (subtask B depends on subtask A)

Execution algorithm:
  0. DAG validation (BEFORE execution begins — Kahn's algorithm):
     - Repeatedly remove nodes with in-degree 0; if any nodes remain at the end,
       the DAG has a cycle.
     - On cycle: workflow status='failed', error_code='INVALID_DAG'; attach the
       offending node set to error_msg for debugging. No subtasks are spawned.
     - Decomposition step (LLM-powered) emits structured DAGs; LLM occasionally
       produces cycles, hence this check is mandatory not optional.
  1. Topological sort of DAG.
  2. Find all nodes with in-degree 0 → execute in parallel (goroutines/async).
  3. When a node completes:
     a. Mark node done via optimistic-locked UPDATE (see workflow_tasks below).
     b. Reduce in-degree of dependent nodes.
     c. Nodes reaching in-degree 0 → execute.
  4. Continue until all nodes done or any node fails.

Failure handling:
  - Subtask fails: mark workflow FAILED, cancel all running subtasks via Component 5b.
  - Subtask timeout: treat as failure.
  - Retry: configurable per subtask (default: 1 retry).

Concurrency model:
  - Subtask completion handlers race on shared workflow_tasks rows (fan-in synthesis nodes).
  - Use optimistic locking: UPDATE ... WHERE id=$id AND version=$prev RETURNING *.
    Version mismatch → re-read row + retry the state-machine step.
  - State changes emit Kafka events via the existing xagent.outbox (Phase 9 post-edit) so
    workflow.completed/failed and Postgres state cannot diverge.
```

**Workflow state stored in PostgreSQL:**
```
xagent.workflows: workflow metadata + status
xagent.workflow_tasks: each node in the DAG with status + output
xagent.outbox:        REUSED (Phase 9 post-edit) for workflow.completed / .failed events
```

```sql
-- Schema additions for the orchestrator:
CREATE TABLE xagent.workflows (
  workflow_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID         NOT NULL,
  root_agent_id  UUID         NOT NULL,                  -- orchestrator agent that owns it
  goal           TEXT         NOT NULL,
  status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
                 -- pending | planning | running | awaiting_approval |
                 -- completed | failed | cancelled
  subtask_dag    JSONB,                                  -- serialised dependency graph
  output         JSONB,
  error_code     VARCHAR(50),
  error_msg      TEXT,
  approval_due_at TIMESTAMPTZ,                            -- for awaiting_approval status
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  timeout_at     TIMESTAMPTZ,                             -- workflow-level timeout
  completed_at   TIMESTAMPTZ,
  version        INTEGER      NOT NULL DEFAULT 1         -- optimistic lock

  -- subtask_dag schema lives in contracts/workflows/dag.schema.json (📋); two
  -- implementations would otherwise diverge on representation.
);
ALTER TABLE xagent.workflows ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflows_tenant_isolation ON xagent.workflows FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TABLE xagent.workflow_tasks (
  id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id       UUID         NOT NULL REFERENCES xagent.workflows(workflow_id),
  tenant_id         UUID         NOT NULL,
  task_id           UUID,                                -- xagent.tasks reference once spawned
  description       TEXT         NOT NULL,
  task_type         VARCHAR(100),
  assigned_agent_id UUID,
  depends_on        UUID[]       NOT NULL DEFAULT '{}',
  status            VARCHAR(20)  NOT NULL DEFAULT 'pending',
                    -- pending | running | completed | failed | cancelled | timeout
  output            JSONB,
  retry_count       INTEGER      NOT NULL DEFAULT 0,
  retry_max         INTEGER      NOT NULL DEFAULT 1,
  version           INTEGER      NOT NULL DEFAULT 1     -- optimistic lock per #10
);
ALTER TABLE xagent.workflow_tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflow_tasks_tenant_isolation ON xagent.workflow_tasks FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Workflow body size cap: subtask_dag + output each ≤ 256 KiB (Contract 3 parity).
-- Larger workflows are a design smell — use sub-workflows.
```

---

### Component 5 — Execution Patterns

**Sequential:**
```
Task 1 → Task 2 → Task 3 → Final Synthesis
```

**Parallel:**
```
              ┌─► Task 1 ─────┐
Goal ─────────┤               ├─► Synthesis
              └─► Task 2 ─────┘
```

**Conditional:**
```
Task 1 ──► [if output.sentiment == "positive"] ──► Task 2a
       └──► [else]                              ──► Task 2b
```

**Loop:**
```
Task 1 ──► [while !complete && iterations < 5] ──► Task 1 (loop)
```

**Human-in-the-loop:**
```
Task 1 ──► [pause: await_approval] ──► Task 2
              (workflow status = awaiting_approval; approval_due_at = NOW() + window)
              POST /v1/workflows/{id}/approve to continue

Approval window:
  - Default: 24 hours (configurable per workflow up to 7 days).
  - On expiry: orchestrator emits a cancel event per Component 5b for all downstream
    subtasks; workflow status = 'cancelled', error_code='APPROVAL_TIMEOUT'.
  - A CronJob (every 60s) sweeps xagent.workflows where status='awaiting_approval'
    AND approval_due_at < NOW(); triggers the cancel path.

Approval auth (MANDATORY — workflow approval is a high-trust action; guessing the
model here is how privilege-escalation bugs ship):

  POST /v1/workflows/{id}/approve
  Headers:
    Authorization:     Bearer <agent-jwt>        ← standard agent JWT, scope: workflow:approve
    X-Approval-Token:  <step-up token>           ← Contract 16 (Phase 2 Component 10)
    X-Request-ID:      <propagated>              ← Contract 8
  Body: { "decision": "approve" | "reject", "comment": "optional reason" }

  Server-side verification (in order; all required):
    1. Standard JWT verify (signature, exp, tenant_id matches workflow's tenant).
    2. Agent JWT scope includes `workflow:approve` (deny-by-default policy).
    3. X-Approval-Token (Contract 16) verified per Phase 2 Component 10:
         - approval_scopes  MUST include 'workflow:approve'
         - approval_resource MUST equal 'workflow:<workflow_id>'  (exact match, NOT '*')
         - one_shot = true (workflow approval is always one-shot)
         - exp + iat checks (TTL ≤ 15 min)
         - jti not previously used (replay check via Valkey `approval-jti:{jti}` SET NX
           EX <ttl>; collision → 409 APPROVAL_TOKEN_REPLAYED)
    4. Workflow status MUST be 'awaiting_approval'; otherwise 409 WORKFLOW_NOT_AWAITING_APPROVAL.
    5. On success:
         - Write `approved_by`, `approved_at`, `approval_decision`, `approval_comment`
           to xagent.workflows (new columns — see schema additions below).
         - Resume orchestrator: status transitions awaiting_approval → running (or
           cancelled if decision='reject', with error_code='APPROVAL_REJECTED').
         - Emit Kafka event cypherx.agent.workflow.approval.recorded via the outbox.

  Schema additions (Phase 10 migration directory; cross-phase):
    ALTER TABLE xagent.workflows
      ADD COLUMN approved_by         UUID,         -- approval_token.approved_by (px0 user UUID)
      ADD COLUMN approved_at         TIMESTAMPTZ,
      ADD COLUMN approval_decision   VARCHAR(10),  -- 'approve' | 'reject'
      ADD COLUMN approval_comment    TEXT;

  Why Contract 16 step-up instead of just the agent JWT:
    - Workflow approval is a HUMAN decision delegated to a px0 user. The agent JWT
      proves an in-cluster service made the call; the approval token proves a
      specific human authorised it. Both are required — defence in depth.
    - Step-up tokens are short-lived (≤ 15 min) and resource-scoped, so a leaked
      one expires fast and only authorises the one workflow named in its claims.

  Out of scope for first cycle (📋):
    - Agent-to-agent approval delegation (an "approver agent" reviewing and
      auto-approving lower-risk workflows). Possible but adds policy complexity
      no first user needs.
```

---

### Component 5b — Cancellation & Timeout Propagation

A workflow cancel or timeout MUST stop all running subtasks, not just refuse to start new ones. Without this, a cancelled workflow can still rack up LLM tokens for minutes.

```
Propagation model (Kafka broadcast — every pod receives every cancel):

1. Cancel signal: DELETE /v1/workflows/{id} OR workflow timeout reached OR approval timeout.
2. Orchestrator (or sweeper) publishes to Kafka topic: cypherx.agent.task.cancel.requested
   payload: {
     workflow_id:   "<uuid>",
     tenant_id:     "<uuid>",            ← REQUIRED; consumers SET LOCAL app.tenant_id from this
     task_ids:      [...],
     reason:        "user_cancel" | "timeout" | "approval_timeout" | "subtask_failed",
     trace_id:      "<uuid>"
   }
3. Every agent-runtime pod consumes this topic with a UNIQUE consumer group per pod:
     consumer_group = "cypherx-agent-cancel-listener-" + RUNTIME_KIND + "-" + POD_NAME
   where RUNTIME_KIND defaults to "xagent" for the platform-built runtime, and is set
   to "external-<vendor>-<runtime>" for external (3rd-party) A2A-compliant runtimes
   participating in delegation chains. This is platform-neutral: external runtimes are
   not forced to inherit xagent's brand in their consumer-group name.
   This gives true fan-out — each pod sees every cancel message. Topic has 12 partitions
   (independent of pod count). The earlier "single consumer group / partitions = N pods"
   model is WRONG: Kafka delivers each message to exactly one consumer per group, which
   means only one pod would see the cancel and the other pods would keep burning tokens.
4. On receipt, each pod:
   a. SET LOCAL app.tenant_id = payload.tenant_id (RLS context).
   b. Checks its in-process running tasks; for any matching task_id:
      - Cancels the LLM call (HTTP/2 RST_STREAM; AbortController/context.Cancel).
      - Cancels any in-flight MCP tool calls.
      - Marks the task status = 'cancelled' in xagent.tasks (RLS-isolated UPDATE).
      - Stops the execution pipeline.
5. For A2A sub-calls already delegated to a downstream agent, the parent also
   POSTs DELETE http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks/{task_id}
   on the receiver's a2a-router endpoint with:
     Authorization:         Bearer <canceller's service-jwt>     ← Phase 7 standard
     X-Forwarded-Agent-JWT: <canceller's current agent-jwt>      ← NOT a chain token
     X-Request-ID:          <propagated>                          ← Contract 8

   Cancel-auth pseudocode (a2a-router, MUST match this verbatim):
     SET LOCAL app.tenant_id = $caller_jwt.tenant_id;
     SELECT delegation_root_agent_id, status
       FROM xagent.tasks
      WHERE task_id = $path.task_id
        LIMIT 1;
     -- 0 rows (incl. cross-tenant — RLS hides) → 404 NOT_FOUND (anti-existence-leak per
     --   Contract 15 test 4).
     IF row.status IN ('completed','failed','cancelled') THEN
       RETURN 200 { "status": row.status, "no_op": true }   -- cancel is idempotent
     END IF;
     IF row.delegation_root_agent_id IS NULL THEN
       -- Task wasn't accepted via A2A — refuse the A2A cancel path (caller should use
       -- /v1/tasks/{id}); 400 INVALID_CANCEL_PATH.
     END IF;
     IF $caller_jwt.agent_id != row.delegation_root_agent_id
        AND 'platform:admin' NOT IN $caller_jwt.scopes THEN
       RETURN 403 FORBIDDEN { "reason": "only_root_agent_or_platform_admin_may_cancel" }
     END IF;
     -- Authorised: re-publish cancel to the Kafka topic with this task_id, then 202.

Topic provisioning (Phase 10 ships this Terraform):
  Topic: cypherx.agent.task.cancel.requested
    partitions: 12, replication: 3, min.insync.replicas: 2,
    cleanup.policy: delete, retention: 1 day
  DLQ:   cypherx.agent.task.cancel.requested.dlq
    partitions: 3, replication: 3, retention: 30 days

Why Kafka (not HTTP):
  - The originating orchestrator does not know which pod is running which subtask.
  - Kafka fan-out (with per-pod consumer groups) reaches every pod with one publish.
  - Cancel is idempotent (already-finished tasks ignore the signal).

Scale note: each pod consumes from all 12 partitions; at >100 pods, consider
re-keying cancels by orchestrator tenant_id so most cancels stay tenant-local
(tunable post-launch).
```

Workflow approval timeout works the same way: when the approval window expires, the orchestrator emits a cancel event for the workflow's downstream tasks.

---

### Component 6 — Distributed Trace Through Orchestration

W3C trace context propagation per Contract 8. Workflow context rides in `tracestate` (vendor-namespaced), NOT in custom headers — `tracestate` propagates by spec; custom headers don't.

```
Every A2A call propagates:
  traceparent:  00-<32-hex-trace-id>-<16-hex-span-id>-<2-hex-flags>
  tracestate:   cypherx=<tenant_id>;wf=<workflow_id>;ptask=<parent_task_id>
  X-Request-ID: <originating request UUID>             ← Contract 8

(tracestate values are vendor-keyed and propagate through any W3C-compliant intermediary.
The previous draft used X-Workflow-ID / X-Parent-Task-ID custom headers — those are NOT
propagated by Istio, Kong, or the OTel SDKs by default, and Tempo/Jaeger don't index
them. Use tracestate.)

`X-Request-ID` provenance (MANDATORY — same rule as Phases 3/4/5/6):
  - = value of inbound `X-Request-ID`. Kong's correlation-id plugin (Phase 1 Component 8)
    injects it on the external request that started the workflow; every hop (orchestrator,
    a2a-router, downstream agent, LLMs gateway, guardrails, RAG, memory) forwards it.
  - Service MUST NOT mint a fresh request_id when the header is present. Fallback +
    WARN log `request_id_generated_fallback=true` if absent (internal-only call path).
  - Never taken from body.
  - This is the single field that lets investigators join across `llms.usage_records`,
    `guardrails.violations`, `rag.documents`, `xagent.tasks`, and the workflow events
    below for a multi-agent investigation. Losing it on the A2A hop breaks every
    downstream trace.

The OTel SDKs map tracestate values to span attributes at receive time:
  cypherx.tenant_id  = <tenant_id>
  cypherx.wf         = <workflow_id>
  cypherx.parent_task= <parent_task_id>

In Tempo: trace shows full tree:
  Orchestrator task
    └── Research Agent subtask (A2A call)
         └── LLM call
         └── Tool call (web-search)
    └── Writer Agent subtask (A2A call)
         └── LLM call

Grafana Tempo TraceQL query to visualise a full workflow:
  { .span_attributes["cypherx.wf"] = "<uuid>" }
```

---

### K8s Deployment Spec — `a2a-router`

```yaml
Namespace:   xagent
Deployment:  a2a-router-v1-0-0                       # version-pinned (Phase 7 convention)
Service:     a2a-router                              # stable cluster DNS for senders;
                                                     # selector points at the version-pinned Deployment
Replicas:    3 fixed                                 # not autoscaled — see note below
Node selector: node-role: agent

Resources:
  requests: { cpu: 500m, memory: 512Mi }
  limits:   { cpu: 1500m, memory: 1Gi }
  # Router CPU is dominated by JWT verification (RS256) on every call. The 500m baseline
  # holds ~150 RPS per pod; bump if benchmarks show otherwise.

Startup probe (informer needs an initial EndpointSlices snapshot before serving):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 24            # 120s grace — first informer sync can be slow on a
                                    # cold cluster with many agent-runtime pods
Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / Auth / agent-runtime.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - PostgreSQL reachable (cancel-auth lookup; xagent.tasks query)
    #   - Auth /livez reachable (JWKS fetch + chain validation)
    #   - At least one agent-runtime endpoint observable in EndpointSlices informer
    #     (router has nothing to route to otherwise)
    # Soft deps (log + metric only):
    #   - Valkey (idempotency + delegation-fanout cardinality cache; missing → fail-open)
    #   - Kafka  (cancel-publish; downstream cancel may be slower if broker is briefly down,
    #             but accept paths still work)

Env vars (from Doppler):
  AUTH_SERVICE_URL              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET      (Contract 12; from service-auth/a2a-router/bootstrap_secret)
  DATABASE_URL                  (PgBouncer → xagent schema, runtime user xagent_user — READ-ONLY for cancel-auth)
  VALKEY_URL                    (idempotency + fanout cardinality)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AGENT_RUNTIME_SERVICE         ("agent-runtime.xagent.svc.cluster.local"; informer watches EndpointSlices of this Service)
  MAX_DELEGATION_DEPTH_DEFAULT  ("5"; tenant overrides come from auth.tenants.delegation_max_depth)
  CANCEL_TOPIC                  ("cypherx.agent.task.cancel.requested")

IRSA: none (no AWS API calls from a2a-router in first cycle).

> **Why 3 fixed replicas, not HPA:** The consistent-hash ring (Component 0) re-shuffles
> whenever the pod set changes. Aggressive autoscaling = constant re-hashing = constant
> agent cache-locality loss. Static 3-per-AZ keeps the ring stable; if throughput
> demands more, bump the static count manually and accept the one-time re-hash. KEDA
> on consumer-group lag is a 📋 candidate once steady-state RPS is known.

> **Service ACL (cross-phase update — Phase 10 migration extends auth.service_acl):**
> - orchestrator → a2a-router        [internal:write]
> - a2a-router → auth-service        [internal:read]    (JWKS + chain validation)
> - a2a-router → agent-runtime       [internal:write]   (forward to selected pod)
> - a2a-router → postgres (via PgBouncer) implicit by DATABASE_URL
> No A2A edge gets an ACL row — A2A is gated by delegation chain (Component 2), NOT ACL.
```

### Cross-phase Auth schema additions (owned by Phase 10's migration directory)

Phase 10 references two Auth-owned configuration surfaces that are not defined elsewhere
(`auth.tenants.delegation_max_depth` per Component 2b, `auth.callback_allowlist` per
Component 1's async-mode SSRF guard). These must exist before Phase 10 deploys.

```sql
-- Per-tenant Auth-side config. Created idempotently on first px0.org.created event
-- (px0-bridge handles seeding). Phase 10 migration creates the TABLE; row population
-- is Phase 11's px0-bridge responsibility.
CREATE TABLE IF NOT EXISTS auth.tenants (
  tenant_id              UUID         PRIMARY KEY,
  delegation_max_depth   INTEGER      NOT NULL DEFAULT 5
                         CHECK (delegation_max_depth BETWEEN 1 AND 10),
  created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Platform-internal — no RLS (Auth service is the sole reader/writer; tenant_id
-- is the row identity, not a filter).

-- Per-tenant async-callback host allow-list. Populated by tenant admin via
-- POST /v1/admin/callback-allowlist (Auth endpoint, scope: tenant:admin or platform:admin).
CREATE TABLE IF NOT EXISTS auth.callback_allowlist (
  tenant_id   UUID         NOT NULL,
  host        TEXT         NOT NULL,                  -- exact host match; no wildcards in first cycle
  added_by    UUID         NOT NULL,                  -- agent_id that ran the POST
  added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, host)
);
ALTER TABLE auth.callback_allowlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY callback_allowlist_isolation ON auth.callback_allowlist FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **Why these belong in Auth (not xagent):** the callback URL host list is a
> per-tenant *trust* decision (where can our async callbacks legitimately go) —
> same shape as JWT issuer or SSO domain config, which all live in Auth. The
> delegation depth limit is an authorisation parameter — also Auth's job.

### Migration ownership — `platform-migrations/phase-10/`

Phase 10 has no local PostgreSQL schema of its own (the orchestrator state lives in
`xagent.workflows` / `workflow_tasks`, which are arguably xagent's). But it contributes
four cross-service migrations:

```
platform-migrations/phase-10/
  ├── 20260801_0900__xagent_tasks_a2a_columns.sql        → xagent.tasks
  │     (delegation_root_agent_id, a2a_callback_url, a2a_callback_secret)
  ├── 20260801_0901__xagent_workflows.sql                → xagent.workflows + workflow_tasks
  │     (Component 4 DDL + approval columns from Component 5)
  ├── 20260801_0902__auth_tenants_and_callback_allow.sql → auth.tenants, auth.callback_allowlist
  ├── 20260801_0903__auth_service_acl_seed.sql           → auth.service_acl
  │     (the 3 service edges listed in the K8s spec ACL block above)
  └── README.md
```

Runtime:
- Applied as Atlas migrations under the platform-admin DDL credential (same pattern
  Phase 5/6/8 already use for cross-service writes).
- Pre-install K8s Job (`helm.sh/hook: pre-install,pre-upgrade`); blocks Phase 10
  deploys if any fail.
- All migrations are idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`
  where supported, `INSERT ... ON CONFLICT DO NOTHING`).

Review:
- CODEOWNERS for `platform-migrations/phase-10/` requires approval from Auth,
  xAgent, AND Platform teams (three-team gate — the cross-service surface is
  bigger than Phase 8's).
- CI runs the migrations against a real Postgres + integration test that walks a
  full A2A delegation, cancel, and approval flow end-to-end.

### Kafka Events

```
cypherx.agent.a2a.delegated
  payload: { orchestrator_agent_id, target_agent_id, task_id, workflow_id, tenant_id,
             delegation_root_task, delegation_depth, chain, request_id, trace_id }

cypherx.agent.workflow.completed
  payload: { workflow_id, tenant_id, subtask_count, total_tokens, total_cost_usd,
             duration_ms, request_id, trace_id }

cypherx.agent.workflow.failed
  payload: { workflow_id, tenant_id, failed_task_id, error_code, error_msg,
             request_id, trace_id }

cypherx.agent.workflow.approval.recorded
  payload: { workflow_id, tenant_id, approved_by, decision, comment,
             request_id, trace_id }
```

> All four events carry `request_id` and `trace_id` so multi-service investigations
> can join across `llms.usage_records`, `guardrails.violations`, `rag.documents`,
> `xagent.tasks` on a single field. Same provenance rule as the headers above —
> never minted if present; fallback + WARN if absent.

---

## ⚡ First Cycle Implementation Checklist

> Phase 10 is entirely 📋 — none of it is first cycle. **Gate:** Phase 9A passes Contract 15 smoke test twice + 7 consecutive days no regressions in dev/staging before Phase 10 kickoff.

## 📋 Full Enterprise Implementation Checklist

- [ ] **Fixed a2a-router endpoint** — every A2A call goes to `http://a2a-router.xagent.svc.cluster.local:8080/v1/a2a/tasks` with `receiver_agent_id` in body envelope; NO per-agent K8s Services
- [ ] **EndpointSlices informer** in a2a-router for consistent-hash ring rebuilds; fallback to random healthy pod on hash miss
- [ ] **`a2a-router` K8s deployment spec** — version-pinned, 3 fixed replicas (no HPA — see ring-stability rationale), startup probe (120s grace for informer sync), readiness gates on PG + Auth + ≥1 agent-runtime endpoint
- [ ] **`xagent.tasks` columns added** — `delegation_root_agent_id` (cancel-auth target), `a2a_callback_url`, `a2a_callback_secret` (raw BYTEA, zeroized on terminal status); NULL for non-A2A tasks
- [ ] **`auth.tenants` and `auth.callback_allowlist` tables created** by Phase 10 migration; row population by px0-bridge (Phase 11); `POST /v1/admin/callback-allowlist` admin endpoint
- [ ] A2A receiver endpoint (full: sync + async + stream modes)
- [ ] A2A sender client (full: sync + async + stream modes)
- [ ] **A2A body size caps** — input ≤ 256 KiB, output ≤ 256 KiB; S3-reference pattern (`cypherx-a2a-output-<env>` bucket, SSE-KMS, 24h lifecycle) for larger payloads
- [ ] **A2A Idempotency-Key** — Valkey-backed `a2a-idemp:{tenant_id}:{receiver_agent_id}:{key}`, 24h TTL; replay returns cached body with `Idempotent-Replay: true`; in-flight → 409; fail-open on Valkey outage
- [ ] **Async callback security** — HTTPS-only, RFC1918/metadata-IP rejection, per-tenant allow-list (`auth.callback_allowlist`); per-task HMAC secret returned in 202 Accepted; sender verifies signature on inbound callback
- [ ] **Async polling auth** — sender uses current service-JWT + agent-JWT (not the short-lived A2A token); router verifies caller's agent_id == chain[0].from
- [ ] A2A JWT validation — **full chain-aware** (Component 2 — chain walk, signature per entry, root_expiry, scope subset, in-chain cycle detection)
- [ ] **Component 2b — Delegation chain semantics** — MAX_DELEGATION_DEPTH (default 5) per-tenant config, transitive flag enforcement, async cross-chain cycle detection via Valkey `a2a-fanout:{root_task}` set, Kafka audit pipeline (`cypherx.agent.a2a.delegated` carries full chain)
- [ ] **Service ACL non-applicability** documented — A2A authorization is delegation_chain, NOT `auth.service_acl`. Only service edges (orchestrator→a2a-router, a2a-router→auth-service, a2a-router→agent-runtime) get ACL rows
- [ ] **Agent Discovery API** — read-only view over `xagent.agents` (NO separate `xagent.agent_registry` table); GIN index on `capabilities` (`jsonb_path_ops`); RLS-isolated
- [ ] **No heartbeat mechanism** — availability is `auth.agents.status=active` AND `xagent.agents.status=active` AND a healthy agent-runtime pod exists (via K8s EndpointSlices)
- [ ] Orchestrator: goal decomposition via LLM
- [ ] **Mandatory DAG cycle validation** via Kahn's algorithm BEFORE execution; cycle → workflow `failed` with `error_code='INVALID_DAG'`
- [ ] Orchestrator: DAG execution (parallel + sequential) with **optimistic locking** (`version` column) on `xagent.workflow_tasks`
- [ ] Orchestrator: conditional execution
- [ ] Orchestrator: loop execution (with iteration cap)
- [ ] Human-in-the-loop: pause, approve, resume; **default approval window 24h, configurable up to 7d**; 60s sweeper cancels on `approval_due_at < NOW()`
- [ ] **`/v1/workflows/{id}/approve` auth** — requires both `workflow:approve` agent JWT scope AND `X-Approval-Token` (Contract 16, Phase 2 Component 10) with `approval_resource = workflow:<id>` (exact match), `one_shot=true`, `jti` replay-check in Valkey; `approved_by`/`approved_at`/`approval_decision`/`approval_comment` written to `xagent.workflows`
- [ ] **A2A cancel-auth pseudocode** in a2a-router — SELECT `delegation_root_agent_id` from `xagent.tasks`; 404 if missing, 200 no-op if terminal, 400 if non-A2A task, 403 unless caller is root agent or `platform:admin`
- [ ] Workflow CRUD API (submit, status, graph, cancel)
- [ ] Workflow state machine (all transitions)
- [ ] **`xagent.workflows` + `xagent.workflow_tasks`** schemas with RLS + optimistic-lock `version` column; `subtask_dag` JSON Schema in `contracts/workflows/dag.schema.json`
- [ ] **Cancellation Kafka topic** `cypherx.agent.task.cancel.requested` (+ DLQ) provisioned via Phase 10 Terraform (`partitions: 12, replication: 3, retention: 1d`)
- [ ] **Cancel consumer groups are PER-POD** (`cypherx-xagent-cancel-listener-<POD_NAME>`) for true fan-out; payload includes `tenant_id` for RLS-context gating
- [ ] **A2A cancel call auth** — canceller uses current service-JWT + agent-JWT; a2a-router verifies caller is `chain[0].from` or holds `platform:admin`
- [ ] **W3C trace propagation** via `tracestate: cypherx=<tenant_id>;wf=<workflow_id>;ptask=<parent_task_id>` (NOT custom `X-Workflow-ID` headers); **`X-Request-ID` forwarded on every A2A hop** (Contract 8 provenance rule — never minted if present; fallback + WARN if absent)
- [ ] Kafka events: `a2a.delegated`, `workflow.completed`, `workflow.failed`, `workflow.approval.recorded` via reused `xagent.outbox` (Phase 9 post-edit); all payloads carry `request_id` + `trace_id`
- [ ] **`platform-migrations/phase-10/`** directory holds the 4 cross-service migrations (xagent.tasks columns, xagent.workflows + workflow_tasks, auth.tenants + auth.callback_allowlist, auth.service_acl seed); CODEOWNERS = Auth + xAgent + Platform; pre-install K8s Job; all idempotent
- [ ] Circuit breaker for A2A calls (per target agent, per caller — same scoping as Phase 9 MCP client)
- [ ] Workflow execution graph API (`GET /v1/workflows/{id}/graph`)
- [ ] A2A retry logic (configurable per subtask)
- [ ] A2A timeout and cancellation propagation

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. A2A Router as Control-Plane Bottleneck — REAL
Evidence: lines 54–92, 724 (3 fixed replicas; no peer path).
**Mitigation (Phase 10.5):** support peer delegation — sender with cached receiver location attempts direct agent-runtime→agent-runtime call before falling back to router. Operator toggle `allow_peer_delegation=true` per tenant.

### 2. Kafka Cancellation Fan-Out Scaling Issue — REAL
Evidence: lines 594–614 (per-pod consumer group); 662–664 (re-key deferred).
**Mitigation:** re-key cancellation topic by `tenant_id` (or `tenant_id:orchestrator_agent_id`) immediately, not post-launch. Measure cancel-latency p99 vs partition cardinality in staging; document SLA pre-deploy.

### 3. Workflow State Contention in PostgreSQL — REAL
Evidence: lines 433–437 (optimistic locking on `workflow_tasks`).
**Mitigation:** high-fanout fan-in (>50 dependents) uses `xagent.workflow_task_aggregates(workflow_id, target_task_id, completed_count, failed_count, version)`. Completers INCR counters; synthesis polls aggregate row. Avoids O(N) lock conflicts.

### 4. Orchestrator Becoming "Temporal-lite" — REAL
Evidence: lines 404–439.
**Mitigation:** explicit decision note — in-house DAG state machine + retries + outbox event-sourcing + timeouts re-implements Temporal/Cadence semantics. Post-Phase-10, audit Temporal OSS for Phase 12 cost-benefit. Owners commit to day-2 maintenance of clock-skew, Kafka rebalance, and idempotency-replay handling.

### 5. Delegation-Chain Validation Latency Growth — REAL
Evidence: lines 242–268 (O(depth) RS256 verifies; MAX_DELEGATION_DEPTH=5).
**Mitigation:** cache validated chains in Valkey — key `a2a-chain:{delegation_root_agent_id}:{hash(chain)}`, TTL = root JWT TTL. Invalidate on root scope/expiry change events.

### 6. Consistent Hashing Hotspot Imbalance — REAL
Evidence: lines 70–86. No per-tenant skew handling.
**Mitigation:** emit `a2a_request_rate{tenant_id,agent_id,pod}/s`. If any (tenant_id, agent_id) pair >100 RPS on single pod >30 s, auto-add virtual nodes for that agent only (round-robin among healthy pods; sacrifice cache-locality for that agent). Manual fallback: increase replicas or set pod affinity.

### 7. Human Approval Operational Complexity — REAL
Evidence: lines 526–585. 24 h default window; no escalation.
**Mitigation:** escalation policy — when `approval_due_at - NOW() < 4 h` and unapproved, emit `approval_escalation_due` and notify backup-approver list (per workflow/tenant). On rejection emit `approval_rejected` and notify root agent. Reduce default approval window to 4 h for Phase 1 deployments (24 h reserved for batch/analysis workflows).

### 8. Agents as Data, Not Infrastructure — VERIFIED (strong decision)
Evidence: lines 57–64. Multi-tenant in-process serving documented with justification.
