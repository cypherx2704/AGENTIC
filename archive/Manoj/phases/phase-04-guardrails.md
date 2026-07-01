# Phase 4 — SharedCore / Guardrails
> **Status:** ⏳ Pending | **Depends On:** Phase 0, 1, 2 | **Blocks:** Phase 9
> **First Cycle:** ⚡ Partial — synchronous input/output check with basic rules required

## Amendment Log (2026-06 — pre-build reconciliation)

- **Policy management CRUD promoted to first cycle (new Component 6b; ⚡/📋 checklists rewritten).** The already-promoted ⚡ features (simulation, custom rules, append-only versioning, `fail_mode_override` paper trail, agent assignment) all hard-depend on tenants being able to author policies. First-cycle surface: `POST /v1/policies` (create), `POST /v1/policies/{id}` (append-only edit: new row, version+1, `previous_policy_id`, atomic `agent_policies` repoint), `PUT /v1/agents/{agent_id}/policy` (assignment); rule-reference + `stream_mode` validation at save; audit-logged `fail_mode_override`; scope `tenant:admin`. Promoted duplicates (CRUD, simulation, custom rules, output-PII) removed from the 📋 checklist.
- **Latency SLOs reconciled with the runtime (SLO section + Components 3/4/5d).** The 30/50 ms and 60/100 ms budgets are unreachable with synchronous Neon round-trips on the hot path. SLOs are restated as **cache-hit SLOs measured at decision-return** with an explicit cold-path allowance; the Valkey 60 s policy cache is now MANDATORY on the hot path; violation/usage persistence moves to a bounded post-response in-process queue draining to the outbox (`guardrails_persist_backlog` metric; overflow → synchronous fallback, never sampled, never dropped).
- **detoxify packaging story added (Component 2 classifier section).** Two image targets: `guardrails:slim` (stub classifier, dev/CI) and `guardrails:ml` (torch-cpu + checkpoint **baked at build with pinned sha256**, no first-use download); **EAGER load in lifespan before readiness** (no lazy load inside the first request/`/readyz`); `CLASSIFIER_MODE`/`CLASSIFIER_THRESHOLD` env-driven; compose prod profile runs `:ml`.
- **Missing contract artifacts now have owners (Components 2/5d/8).** `contracts/guardrails/jailbreak-leak-patterns.md`, `contracts/guardrails/golden-suite.jsonl` (seeded from existing test fixtures), and `contracts/billing/guardrails-rule-cost.md` (matching `rules.estimated_cost_usd_per_call`) are authored under `contracts/` in WP01 by the contracts pass. Custom rules narrowed to `regex` + `classifier-threshold` first cycle (`semantic` gated on WP06 embeddings; `chain` deleted).
- **X-Request-ID hardened (Component 4).** Trace middleware validates the inbound header as a UUID; absent OR non-UUID → synthesize UUIDv4 with `request_id_generated_fallback=true`. Violation/billing rows are ALWAYS written — a junk header from an external caller can no longer suppress its own rows (closes the fail-soft UUID-cast drop). CI junk-header test required.
- **Compose-parity (deployment section + checklist split; redaction key_ref loosened).** First cycle builds against compose + Neon + Valkey + Redpanda + MinIO — no AWS/K8s/Kong/px0. K8s/ArgoCD/Doppler items restated as compose equivalents (cloud forms kept in a Deploy-target checklist). `tenant_redaction_keys.key_ref` literal `CHECK (LIKE 'secretsmanager:%')` dropped in favor of the pluggable secret-backend registry pattern (`sealed:v1:` / `env:` first cycle; `secretsmanager:` future cloud backend), app-side validated.
- **Minor batch (WP01 doc pass).** Check endpoints are 200-always (decision in body; the CALLER mints 422 GUARDRAIL_VIOLATION); `REDACTION_HMAC_KEY_PLATFORM` name standardized everywhere; usage `operation` enum moved to dot-notation (`check.input` / `check.output`; `check.both` removed — `/check/both` was already dropped from first cycle); `output-max-length-v1` gets per-rule `params` `{max_chars}` DB-seeded; `violations.rule_id` FK to `guardrails.rules` replaced with app-side validation.
- **Golden-suite size relaxed to the authored artifact (Component 6b validation pipeline).** The CI-lint baseline was specified as "100 known-input/known-decision pairs" but the WP01-authored `contracts/guardrails/golden-suite.jsonl` ships with >=55 pairs, growing. The pipeline now references the artifact's actual (growing) size rather than a fixed count; gating semantics unchanged.

---

## Phase Overview

SharedCore/Guardrails is the **AI safety and policy enforcement layer**. Every prompt going into an LLM and every response coming out can be passed through this service before being acted on. It is a first-cycle requirement because xAgent must check inputs and outputs before processing.

**Deliverable:** A running guardrails service that synchronously checks text for PII, prompt injection, and toxicity, with a simple block/warn/allow decision. Policies are defined as YAML files per tenant.

> 🏗️ **Service Architecture Note:** The internal architecture of the guardrails service (ML model choices, classifier integration, rule engine internals, async queue design) must be planned separately before implementation begins.

---

## High Level Design

### System Context

```
                        ┌──────────────────────────────────────────┐
                        │         GUARDRAILS SERVICE               │
                        │                                          │
  xAgent (pre-LLM) ────►│  POST /v1/check/input   (sync)          │
  xAgent (post-LLM) ───►│  POST /v1/check/output  (sync, buffered) │
                        │  GET  /v1/policies                       │
                        │  POST /v1/policies                       │
                        │  POST /v1/policies/{id}      (edit)     │
                        │  PUT  /v1/agents/{id}/policy (assign)   │
                        │  POST /v1/policies/{id}/simulate        │
                        └──────────────┬───────────────────────────┘

> xAgent is the SOLE caller in first cycle. LLMs Gateway is a transparent proxy and
> MUST NOT call guardrails — that would double-enforce the same policy and split
> responsibility for "what got blocked, where". The /check/both endpoint is dropped
> from first cycle (no use case: pre-check sees only input; post-check sees both
> but input was already checked one round-trip ago).
                                       │
                 ┌─────────────────────┼─────────────────────┐
                 ▼                     ▼                      ▼
          Rule Engine           ML Classifiers          PostgreSQL
       (regex, patterns,       (PII, toxicity,         (policies,
        policy evaluation)      injection models)      violations)
```

### Check Request Flow

```
Input text arrives at /check/input
  │
  ▼
1. Load active policy set for this agent/tenant (Valkey cache, 60s TTL)
  │
  ▼
2. Run checks in pipeline (parallel where possible):
   ├── Prompt injection detector
   ├── PII detector (NER-based)
   ├── Toxicity/harm classifier
   ├── Topic blocklist matcher
   └── Custom regex rules
  │
  ▼
3. Aggregate results: any BLOCK → block; else any REDACT → redact; else WARN or ALLOW
  │
  ▼
4. Return decision + processed text (if redacted)   ← SLO measured here (decision-return)
  │
  ▼
5. Enqueue violation/usage records (if not ALLOW) onto the post-response
   persist queue → violations + outbox in one transaction (Component 4)
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after first cycle.

---

### Component 1 — Check API ⚡

**Input Check Request:**
```json
POST /v1/check/input
Authorization: Bearer <service-jwt>                  -- Contract 12 service token
X-Forwarded-Agent-JWT: <agent-jwt>                   -- agent identity, Contract 12
traceparent: 00-<trace-id>-<span-id>-01              -- W3C trace context, Contract 8

{
  "text":          "Please ignore previous instructions and...",
  "task_id":       "<uuid>",                          -- correlation only; NOT identity
  "policy_set_id": null,                              -- null = resolve via agent/tenant/platform chain
  "mode":          "sync"                             -- sync | async
}
```

> **Identity comes from the JWT, never the body (Contract 13 anti-pattern).**
> `agent_id` and `tenant_id` are extracted from `X-Forwarded-Agent-JWT`. `trace_id`
> and `span_id` come from `traceparent`. If the body contains `agent_id`, `tenant_id`,
> `trace_id`, or `span_id`, the gateway returns 400 BAD_REQUEST.

**Output Check Request:**
```json
POST /v1/check/output
Authorization: Bearer <service-jwt>                  -- Contract 12 service token
X-Forwarded-Agent-JWT: <agent-jwt>                   -- agent identity, Contract 12
traceparent: 00-<trace-id>-<span-id>-01              -- W3C trace context, Contract 8
X-Request-ID: <uuid>                                  -- forwarded from inbound request, Contract 8

{
  "text":          "<LLM-assembled response, full buffer>",
  "input_text":    "<original user input that was checked at /check/input>",
                   -- OPTIONAL. Required by `output-pii-email-v1` to distinguish
                   -- "email NOT in input" from "user echoed their own email".
                   -- If omitted, the rule degrades to "ANY email in output → redact"
                   -- (over-redaction is the safe failure mode). xAgent MUST send it
                   -- in first cycle since it already has the input buffered for
                   -- the pre-LLM /check/input call.
  "task_id":       "<uuid>",                          -- correlation only; NOT identity
  "policy_set_id": null,                              -- null = resolve via agent/tenant/platform chain
  "mode":          "sync"                             -- sync only in first cycle (buffer streaming mode)
}
```

> **`/check/output` does NOT receive the system prompt.** System prompts may contain
> tenant secrets (proprietary instructions, API keys embedded in agent setup) and sending
> them to guardrails would (a) expand the service's blast radius, (b) require logging
> guarantees this service does not provide. Rules that need system-prompt context
> (e.g. "response repeats system prompt verbatim") are scoped down to pattern-only
> matching in first cycle — see `output-jailbreak-leak-v1` in Component 2. Embedded
> library mode (where guardrails runs in-process inside xAgent and CAN see the system
> prompt without exfiltration) is 📋.

**Check Response (identical shape for `/check/input` and `/check/output`):**
```json
{
  "decision":    "block",           -- allow | warn | redact | block
  "processed_text": null,           -- populated if decision = redact
  "violations": [
    {
      "rule_id":    "pii-email-v1",
      "rule_name":  "PII Email Detector",
      "severity":   "medium",
      "category":   "pii",
      "matched":    "[REDACTED:email:9f3a2b1c]",     -- SAFE for callers to log; see redaction rule below
      "action":     "redact"
    },
    {
      "rule_id":    "prompt-injection-v1",
      "rule_name":  "Prompt Injection Detector",
      "severity":   "critical",
      "category":   "security",
      "matched":    "ignore previous instruct",      -- ≤64-char truncation for non-PII categories
      "action":     "block"
    }
  ],
  "check_id":    "<uuid>",
  "duration_ms": 18,
  "trace_id":    "<uuid>"
}
```

> **API-response `matched` MUST be redacted/truncated to the same rule as DB storage**
> (Component 4 `matched_text` content rule). Without this, raw PII flows back to xAgent,
> ends up in Contract 6 structured logs, and Loki becomes the easiest cross-tenant PII
> store on the platform — defeating the storage rule entirely. Callers MUST be able to
> log `violations[].matched` verbatim without leaking PII. CI test covers both write
> paths (DB row AND HTTP response) with the same fixture per category.

---

### Component 2 — Rule Definitions ⚡

**First cycle INPUT rules (must be implemented and always-on for platform default policy):**

| Rule ID | Type | What it detects | Default Action |
|---------|------|----------------|----------------|
| `prompt-injection-v1` | Regex + heuristic | "ignore previous", "disregard instructions", "new prompt:", DAN patterns | block |
| `pii-email-v1` | Regex | Email addresses in input | redact |
| `pii-phone-v1` | Regex | Phone numbers | redact |
| `pii-credit-card-v1` | Regex + Luhn | Credit card numbers | block |
| `toxicity-v1` | ML classifier | Hate speech, threats, self-harm | block |
| `jailbreak-v1` | Regex + keyword | Common jailbreak phrases | block |

**First cycle OUTPUT rules (⚡ required — output check is meaningless without them):**

| Rule ID | Type | What it detects | Default Action |
|---------|------|----------------|----------------|
| `output-pii-email-v1` | Regex | LLM response contains an email address that was NOT in the input | redact |
| `output-pii-credit-card-v1` | Regex + Luhn | LLM response contains a credit card number | block |
| `output-toxicity-v1` | ML classifier (same model as input toxicity) | Hate speech, threats, self-harm | block |
| `output-jailbreak-leak-v1` | Regex | Response contains known reveal patterns ("I am an AI language model", "my instructions are", "my system prompt", and the full set published in `contracts/guardrails/jailbreak-leak-patterns.md` — artifact authored in WP01 by the contracts pass). **Pattern-only — does NOT compare against the actual system prompt** (which the service intentionally never receives; see `/v1/check/output` note above). System-prompt-aware detection is 📋 (embedded-library mode). | block |
| `output-max-length-v1` | Count | Response exceeds the rule's configured `params.max_chars` (per-rule `params` JSONB on `guardrails.rules`, DB-seeded — not hardcoded) | block |

**ML classifier — FIRST CYCLE LOCKED (packaging story added — see Amendment Log):**
- **`detoxify` (small RoBERTa, ~250 MB)** loaded **in-process** inside guardrails-service for English toxicity classification. One pod = service + model — simplest ops, no extra network hop, no extra failure mode.
- **Two image targets (the packaging story):**
  - `guardrails:slim` — keyword-stub classifier (`CLASSIFIER_MODE=stub`); dev/CI image where torch's ~2 GB footprint is unjustified.
  - `guardrails:ml` — torch-cpu + the detoxify checkpoint **baked into the image at build time with a pinned sha256** (NEVER downloaded at first use from GitHub). `CLASSIFIER_MODE=detoxify`. The compose **prod profile runs `:ml`**.
- **EAGER load in the service lifespan, BEFORE readiness reports ready** — never lazy-loaded inside the first request or the first `/readyz` call. A pod that cannot load its model never becomes ready (prod can no longer silently run the keyword stub).
- Classification threshold is env config (`CLASSIFIER_THRESHOLD`), not hardcoded.
- Code calls a private in-process function `Classify(text) → []Category`, NOT an HTTP endpoint. The function signature is the abstraction boundary so the model can be swapped without touching rule code.
- **📋 follow-ups:**
  - Llama Guard 2 (8B) / Prompt Guard (86M) for richer taxonomy; deployed as a SEPARATE inference pod once the classifier becomes the throughput bottleneck.
  - Multilingual extension once tenants outside English appear.

**Latency SLO (first cycle, enforced via Prometheus alert — CACHE-HIT SLO, measured at decision-return):**

The budgets below apply to the **policy-cache-hit path** and are measured at the
moment the decision is returned to the caller — NOT after violation/usage
persistence, which is post-response (Component 4 persist queue). The Valkey
policy cache (60 s TTL, Component 3) is therefore **MANDATORY on the hot path**,
not an optimization: a synchronous Neon round-trip for policy resolution cannot
fit inside these budgets.

| Endpoint            | p95 (cache hit) | p99 (cache hit) | Cold-path allowance (cache miss / Valkey down) |
|---------------------|-------|-------|------------------------------------------------|
| `/check/input`      | 30 ms | 50 ms | p99 ≤ 250 ms (one Neon policy-resolution round-trip) |
| `/check/output` (buffer mode) | 60 ms | 100 ms | p99 ≤ 250 ms |

Cold-path requests carry a `cache_hit="false"` metric label and are EXCLUDED from
the cache-hit SLO alert series; a separate alert fires when cold-path traffic is
sustained (> 1 % of checks over 15 min — the cache is failing at its job).
Breach of the cache-hit SLO for ≥ 5 min fires Alertmanager. xAgent pays 2× this latency per task
(pre + post), so the budget must stay tight from day one.

**Rule version semantics (first cycle):**
Rule IDs (`prompt-injection-v1`) are **compiled-in code**. The `guardrails.rules`
registry table (below) is the source of truth at the DB level. Adding a new
version (`prompt-injection-v2`) requires a code release + a migration that inserts
the new row and marks the old one `status='deprecated'`. Policies reference
specific versioned IDs — no implicit "latest" semantics.

**Full enterprise rules (📋 design now, implement after first cycle):**

| Rule ID | Type | What it detects |
|---------|------|----------------|
| `pii-ssn-v1` | Regex | US Social Security Numbers |
| `pii-passport-v1` | Regex | Passport numbers |
| `pii-name-v1` | NER model (spaCy / Presidio) | Full person names |
| `topic-blocklist-v1` | Semantic | Similarity to blocked topics |
| `output-hallucination-v1` | LLM judge | Claims without supporting evidence |
| `output-format-v1` | JSON schema | Response doesn't match expected format |

---

### Component 3 — Policy Engine ⚡

**What it is:** Groups rules into named policy sets. Each agent or tenant has an assigned policy set.

**Policy definition (YAML, stored in PostgreSQL as JSON):**
```yaml
policy_id: default-platform-policy
version: 1.0.0
name: Platform Default Policy
description: Applied to all agents unless overridden

rules:
  - rule_id: prompt-injection-v1
    enabled: true
    action_override: null    # null = use rule default
  - rule_id: pii-email-v1
    enabled: true
    action_override: redact
  - rule_id: pii-credit-card-v1
    enabled: true
    action_override: block
  - rule_id: toxicity-v1
    enabled: true
    action_override: block
  - rule_id: jailbreak-v1
    enabled: true
    action_override: block
```

**PostgreSQL (`guardrails.rules` — registry, source of truth for rule IDs):**
```sql
CREATE TABLE guardrails.rules (
  rule_id              VARCHAR(100) PRIMARY KEY,    -- e.g., "prompt-injection-v1"
  version              VARCHAR(20)  NOT NULL,
  default_action       VARCHAR(20)  NOT NULL,        -- allow | warn | redact | block
  default_fail_mode    VARCHAR(20)  NOT NULL DEFAULT 'closed',
  default_stream_mode  VARCHAR(20)  NOT NULL DEFAULT 'buffer',
  default_severity     VARCHAR(20)  NOT NULL,        -- info | low | medium | high | critical
  default_category     VARCHAR(50)  NOT NULL,        -- security | pii | toxicity | jailbreak | length
  direction            VARCHAR(10)  NOT NULL,        -- input | output | both
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | deprecated
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Platform-scoped (no tenant_id, no RLS). Seed migration inserts all first-cycle rule IDs.
-- Policies reference these rule_ids; application validates references at policy save time.
```

**PostgreSQL (`guardrails.policies`):**
```sql
CREATE TABLE guardrails.policies (
  policy_id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID,                       -- NULL = platform default
  name                 VARCHAR(255) NOT NULL,
  version              INTEGER      NOT NULL DEFAULT 1,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',
                       -- active | superseded | deprecated
  rules                JSONB        NOT NULL,     -- array of rule configs (references rules.rule_id)
  is_default           BOOLEAN      NOT NULL DEFAULT false,
  previous_policy_id   UUID REFERENCES guardrails.policies(policy_id),  -- append-only chain
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Only one default per (tenant_id) — enforced at DB level:
CREATE UNIQUE INDEX policies_one_tenant_default
  ON guardrails.policies(tenant_id)
  WHERE is_default = true AND tenant_id IS NOT NULL AND status = 'active';
CREATE UNIQUE INDEX policies_one_platform_default
  ON guardrails.policies((1))
  WHERE is_default = true AND tenant_id IS NULL AND status = 'active';

-- Mixed-scope RLS (read platform defaults; write only own tenant) — same pattern as Phase 3:
ALTER TABLE guardrails.policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY policies_read ON guardrails.policies FOR SELECT
  USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);
CREATE POLICY policies_write ON guardrails.policies FOR ALL
  USING      (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TABLE guardrails.agent_policies (
  agent_id     UUID NOT NULL,
  tenant_id    UUID NOT NULL,
  policy_id    UUID NOT NULL REFERENCES guardrails.policies(policy_id),
  PRIMARY KEY (agent_id, tenant_id)
);
ALTER TABLE guardrails.agent_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY agent_policies_isolation ON guardrails.agent_policies FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **Append-only policy versioning:** Updates do NOT mutate `policies.rules`. Each
> edit inserts a NEW row with a new `policy_id`, `version = old + 1`, and
> `previous_policy_id = old.policy_id`. The old row transitions
> `status = 'superseded'`. `agent_policies.policy_id` is repointed atomically in
> the same transaction. Old rows kept forever — violations log carries
> `policy_id`, so "what policy applied at time T?" is always reconstructible.

> **Policy resolution chain (single SQL, in order):**
> 1. `agent_policies row` for (agent_id, tenant_id) → use that `policy_id`.
> 2. Tenant default: `policies WHERE tenant_id = $1 AND is_default = true AND status = 'active'`.
> 3. Platform default: `policies WHERE tenant_id IS NULL AND is_default = true AND status = 'active'`.
> If step 3 yields nothing, the service refuses to start (`/readyz` returns 503 with reason `no platform default policy`).

> **Hot-path caching (MANDATORY — see SLO section):** the resolution-chain SQL runs
> only on a Valkey policy-cache miss (60 s TTL). Cache-hit checks never touch Neon
> before returning a decision. Valkey outage does NOT fail readiness — checks fall
> through to the DB at cold-path latency and the sustained-cold-path alert fires.
> Policy writes (Component 6b) become visible to enforcement within one refresh
> cycle (≤ 60 s).

---

### Component 4 — Violation Log ⚡

**PostgreSQL (`guardrails.violations`):**
```sql
CREATE TABLE guardrails.violations (
  id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  check_id     UUID         NOT NULL,
  request_id   UUID         NOT NULL,                  -- = inbound X-Request-ID (see provenance note below)
  tenant_id    UUID         NOT NULL,
  agent_id     UUID         NOT NULL,
  task_id      UUID,                                    -- nullable: non-task callers (e.g., admin probes)
  trace_id     UUID         NOT NULL,                  -- Contract 8 mandates traceparent on every request
  policy_id    UUID         NOT NULL REFERENCES guardrails.policies(policy_id),
  direction    VARCHAR(10)  NOT NULL,                  -- input | output (renamed from check_type for Contract 5 parity)
  decision     VARCHAR(10)  NOT NULL,                  -- allow | warn | redact | block
  rule_id      VARCHAR(100) NOT NULL,                  -- app-side validated against guardrails.rules (FK dropped — see Amendment Log)
  rule_name    VARCHAR(255) NOT NULL,
  severity     VARCHAR(20)  NOT NULL,
  category     VARCHAR(50)  NOT NULL,
  matched_text TEXT,                                    -- see redaction rule below
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_violations_tenant_id  ON guardrails.violations(tenant_id, created_at DESC);
CREATE INDEX idx_violations_agent_id   ON guardrails.violations(agent_id, created_at DESC);
CREATE INDEX idx_violations_rule_id    ON guardrails.violations(rule_id, created_at DESC);
CREATE INDEX idx_violations_request_id ON guardrails.violations(request_id);  -- cross-service correlation with LLM call

ALTER TABLE guardrails.violations ENABLE ROW LEVEL SECURITY;
CREATE POLICY violations_isolation ON guardrails.violations FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> Note: PK switched from `BIGSERIAL` to `UUID` — a global sequence leaks
> cross-tenant violation rates via the integer value (side channel).

> **`request_id` and `trace_id` provenance (MANDATORY — same rule as Phase 3 LLMs gateway):**
> - `request_id` = value of the inbound `X-Request-ID` header (compose runtime: injected by
>   the callers, xAgent / the BFF; cloud form: Kong's `correlation-id` plugin, Phase 1
>   Component 8, injects it at the edge). xAgent forwards it on the internal call to
>   guardrails (Contract 8). The service MUST NOT mint its own request_id when a VALID
>   header is present. The trace middleware **validates the header as a UUID**: if it is
>   absent OR not a parseable UUID, generate a UUIDv4, set `X-Request-ID` on any outbound
>   calls, and emit a WARN log `request_id_generated_fallback=true`. The violation and
>   usage rows are ALWAYS written with the synthesized id — a non-UUID header from an
>   external caller MUST NOT suppress its own violation/billing rows (no fail-soft
>   UUID-cast drop). CI test sends a junk `X-Request-ID` and asserts both rows still land.
> - `trace_id` = the 16-byte trace ID parsed from `traceparent` (Contract 8). Stored as UUID
>   (same 128-bit width). The service MUST NOT proceed without a trace ID — if `traceparent`
>   is missing, synthesize one, log WARN, and start a new trace rather than write NULL.
> - Neither field is ever taken from the request body. Service rejects with 400 if a body
>   field named `request_id` or `trace_id` appears.
> - Carrying `request_id` lets investigators join `guardrails.violations` ↔ `llms.usage_records`
>   on a single value, instead of guessing via `(task_id, created_at window)`.

> **`matched_text` content rule (MUST enforce — otherwise the violation log
> becomes the easiest cross-tenant PII store on the platform):**
> - If `category ∈ {pii, email, phone, credit_card, ssn, name, passport}` →
>   store ONLY the redaction token (same `[REDACTED:<category>:<hmac>]` format
>   as Component 5). The raw matched substring is dropped before INSERT.
> - If `category ∈ {security, toxicity, jailbreak, length, other}` →
>   store the first 64 chars of the matched substring (truncate). Never more.
> - CI test loads the violation writer with a fixture for each category and
>   asserts the stored `matched_text` matches the rule above. PR cannot merge
>   without this test passing.

**Outbox pattern (REQUIRED — same rationale as Phase 3):**

```sql
CREATE TABLE guardrails.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished
  ON guardrails.outbox(created_at) WHERE published_at IS NULL;
```

Write path (POST-RESPONSE — persistence is OFF the decision path; see SLO section):

The check handler returns the decision FIRST, then enqueues the violation/usage
records onto a bounded in-process persist queue. A per-pod writer drains the
queue and performs, per record, one transaction:
```
BEGIN;
  INSERT INTO guardrails.violations (...) VALUES (...);
  INSERT INTO guardrails.outbox (topic, partition_key, payload) VALUES
    ('cypherx.guardrails.violation.detected', tenant_id::text, <Contract 5 envelope JSON>);
COMMIT;
```

Persist-queue semantics (explicit — violations/billing are NEVER sampled, never dropped):
- Bounded buffer (default 10,000 records; env `PERSIST_QUEUE_MAX`).
- `guardrails_persist_backlog` gauge exported; alert on sustained growth.
- **Overflow semantics:** when the queue is full, new checks fall back to
  SYNCHRONOUS inline persistence (same transaction shape, on the response path) —
  latency is sacrificed (the SLO alert fires), data is not. Records are never shed.
- Graceful shutdown drains the queue before process exit.

Publisher loop: one goroutine per pod, batch SELECT 100, publish with partition_key,
mark published_at, exponential backoff on failure, DLQ to
`cypherx.guardrails.violation.detected.dlq` after 10 attempts.
A nightly job deletes `outbox` rows where `published_at < NOW() - INTERVAL '7 days'`.

**Kafka event payload (matches Contract 5 first-cycle spec):**
```json
Topic: cypherx.guardrails.violation.detected
Partition key: tenant_id
Payload (inside Contract 5 envelope's "payload" field):
{
  "check_id":   "<uuid>",
  "request_id": "<uuid>",             // = inbound X-Request-ID; joins to llms.usage_records.request_id
  "tenant_id":  "<uuid>",
  "agent_id":   "<uuid>",
  "task_id":    "<uuid>",
  "policy_id":  "<uuid>",
  "direction":  "input",              // renamed from check_type — Contract 5 parity
  "decision":   "block",
  "rule_id":    "prompt-injection-v1",
  "severity":   "critical",
  "trace_id":   "<uuid>"              // required by Contract 5
}
```

---

### Component 5 — Enforcement Modes ⚡

| Mode | Behaviour |
|------|-----------|
| `block` | Check endpoint returns **200 with `decision: "block"`** (the check itself succeeded — check endpoints are 200-always). The **caller** (xAgent) rejects its own request and mints the 422 GUARDRAIL_VIOLATION toward its client. Text not processed downstream. |
| `redact` | Remove the violating part, return cleaned text. Request continues. |
| `warn` | Allow. Flag violation in response (`"warnings": [...]`). Violation logged. |
| `audit` | 📋 Allow without flagging in response. Violation logged for offline review. |

**Standard redaction format (⚡ — must be deterministic so post-processing can match):**
```
PII redaction replaces the match with: [REDACTED:<category>:<token>]
where:
  category = email | phone | credit_card | ssn | name | passport
  token    = first 8 hex chars of HMAC-SHA256(
                key  = REDACTION_HMAC_KEY_PLATFORM        (per-env secret; or the tenant's own key),
                data = tenant_id || ":" || matched_text
             )

Example (tenant T):
  input:  "Contact me at alice@example.com or 555-1234"
  output: "Contact me at [REDACTED:email:9f3a2b1c] or [REDACTED:phone:7d4e8a01]"

Properties:
  - Same value, same tenant → same token (downstream LLM can treat occurrences consistently
    as "the same entity").
  - Same value, different tenant → different token (no cross-tenant linkability).
  - Same value, after a key rotation → different token (rotation invalidates the link).
  - Without the HMAC key the token is not invertible — even a holder of the violations table
    cannot brute-force common emails.

REDACTION_HMAC_KEY_PLATFORM (PER-TENANT — first-cycle, with platform fallback;
name standardized — the bare `REDACTION_HMAC_KEY` spelling is retired):
  - Default: a per-env REDACTION_HMAC_KEY_PLATFORM 32-byte key supplied via env
    (compose env_file/.env; Doppler `guardrails/redaction_hmac_key_platform` is the
    cloud form). Used for tenants that have not opted into BYO redaction key.
  - Per-tenant override: tenants on `pro`/`enterprise` plans MAY register their own
    32-byte redaction key, stored via the pluggable secret-backend registry
    (same pattern as the Phase 3 BYOK amendment). First-cycle backends:
      `sealed:v1:<uuid>` — raw key AES-256-GCM envelope-encrypted in Postgres under
                           an env-supplied KEK (`REDACTION_KEK`), the same pattern
                           Auth already uses for signing keys;
      `env:<VAR>`        — platform-operated keys.
    `secretsmanager:<arn>` (AWS Secrets Manager + per-tenant KMS CMK alias
    `alias/cypherx-guardrails-<env>`) remains an OPTIONAL FUTURE cloud backend.
    Reference held in `guardrails.tenant_redaction_keys.key_ref`
    (backend-qualified; validated app-side against the backend registry).
  - Resolution: gateway middleware looks up `guardrails.tenant_redaction_keys` by
    `tenant_id`. Cache hit → use tenant key. Miss → use platform key.
  - Rotation: tenant can `POST /v1/redaction-keys/rotate` (tenant:admin scope).
    Old key marked `pending` for 30 days so existing redacted tokens remain matchable;
    new redactions use `current`. Background job deletes `pending` after grace.
  - The platform key MUST also be rotatable; tenant-level rotation cadence is the
    tenant's choice (HIPAA customers may rotate monthly; SOC 2 customers quarterly).
```

**Data Model addition:**
```sql
CREATE TABLE guardrails.tenant_redaction_keys (
  tenant_id   UUID NOT NULL,
  key_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key_ref     TEXT NOT NULL,  -- backend-qualified: 'sealed:v1:<uuid>' | 'env:<VAR>' | future 'secretsmanager:<arn>';
                              -- app-side validated against the secret-backend registry (literal CHECK dropped — see Amendment Log)
  status      VARCHAR(20) NOT NULL DEFAULT 'current'
              CHECK (status IN ('current', 'pending', 'retired')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at  TIMESTAMPTZ
);
CREATE INDEX ix_tenant_redaction_current
  ON guardrails.tenant_redaction_keys (tenant_id)
  WHERE status = 'current';
ALTER TABLE guardrails.tenant_redaction_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant_redaction_keys ON guardrails.tenant_redaction_keys
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> Why per-tenant keys: a HIPAA customer's PHI redaction tokens must not be linkable across tenants — even more importantly, if our platform key is compromised, the customer's PII tokens must remain protected by their own key. Single platform key = single point of failure for every customer's redacted data.

---

### Component 5b — Service Availability Decision (Fail-Open vs Fail-Closed) ⚡ (NEW)

**The most important security decision in the guardrails service:** what does an agent do when guardrails is unreachable, slow, or returns an error?

| Mode | Behaviour on Guardrails failure |
|------|--------------------------------|
| **fail-closed** | Block the request (return 503 SERVICE_UNAVAILABLE) |
| **fail-open with warn** | Allow the request, log a `guardrails_bypassed` event with reason |
| **circuit-breaker fail-open** | After N consecutive failures, briefly fail-open with monitoring alert; restore fail-closed once health recovers (used inside agent runtime to avoid cascading failures) |

**FIRST-CYCLE DECISION — all rules default `fail_mode: closed`.**

The prior draft made `fail_mode` depend on tenant plan (open for free-tier, closed
for pro/enterprise). That is backwards: free-tier is precisely the population you
trust LEAST. It also creates a 2-D matrix of behaviours (plan × rule) that is hard
to test and hard to audit.

Rules:
- Every rule's `default_fail_mode` is `closed` (encoded in `guardrails.rules`).
- A tenant or platform admin MAY override per-policy via an explicit `fail_mode_override`
  field on a rule entry inside `policies.rules`. That override is a paper-trailed
  decision — surfaced in the policy diff at PR time and in the audit log on save.
- Circuit-breaker mode lives in the xAgent runtime, NOT in policy. xAgent trips the
  breaker on N consecutive guardrails 5xx; while tripped, it fails-open for non-block
  rules only (block-default rules still fail-closed) and emits
  `cypherx.guardrails.breaker.tripped` to Kafka. Phase 9 owns the implementation.

This is non-negotiable for first cycle because the answer must be in code before
the first production traffic flows — flipping it later is a security review event.

---

### Component 5c — Streaming Output Guardrails ⚡

Output guardrails on streamed LLM responses are inherently complex: by the time a violation is detected, partial unsafe content may already have been delivered. Three strategies exist, but only one is in scope for first cycle.

| Strategy | How | Latency cost | First cycle? |
|----------|-----|--------------|--------------|
| **Buffer-then-check** (`stream_mode: buffer`) | xAgent buffers the full response in memory, calls `POST /v1/check/output` once at end. Guardrails returns block/allow/redact. xAgent then emits the response (or the redacted version) to the client in one go, OR sends a single `data:{"type":"error", ...}` if blocked. | Loses streaming UX; +~LLM-latency seconds before the first client token. | ⚡ **YES — only mode for first cycle.** |
| **Window-check** (`stream_mode: window`) | Maintain a 256-token sliding window; run regex rules every N tokens; emit `data:{"type":"error"}` and close stream on violation. | +~5–20 ms per check; preserves streaming. | 📋 — requires bi-directional streaming RPC plumbing between xAgent and guardrails OR an embedded library; neither belongs in first cycle. |
| **Post-only** (`stream_mode: post`) | Forward tokens immediately; on stream end, run heavy rules; on violation, emit trailing `data:{"type":"warning"}` event. | Zero added latency; no mid-stream protection. | 📋 — `warn`-only audit use case; out of scope for first cycle. |

Each rule definition includes `stream_mode: buffer | window | post`, but the runtime
ignores anything other than `buffer` in first cycle and emits a startup error if a
policy references `window` or `post` (so misconfigured deployments fail fast).

> **Wire pattern is plain HTTP, NOT streaming RPC.** xAgent calls `POST /v1/check/output`
> with the full assembled text body. Per-token streaming to a remote guardrails service
> requires bidirectional gRPC or chunked SSE upload, plus careful flow control — all of
> which are 📋 follow-ups, not first-cycle work.

---

### Component 5d — Rate Limiting & Per-Check Usage Metering ⚡ (NEW)

Guardrails was previously rate-limit-free because xAgent was the only caller. As soon as external developers can call `/v1/check/*` directly (per Contract 18 + 19), the in-process detoxify model (250 MB resident, single-thread bound) becomes a DoS target. Two protections required from first cycle:

**1. Per-tenant rate limit (Contract 19 quotas):**

| Plan | `checks_per_min` | `input_bytes_per_min` | `custom_rules_max` |
|------|------------------|----------------------|--------------------|
| free | 60 | 1 MiB | 0 (platform rules only) |
| pro | 1000 | 10 MiB | 50 |
| enterprise | 10000 | 100 MiB | unlimited |

Sliding-window counter in Valkey, key `quota:guardrails:{tenant_id}:{window}`. Atomic Lua script increments + checks. Breach → 429 `RATE_LIMIT_EXCEEDED` with Contract 2 `X-RateLimit-*` headers. Fail-closed on Valkey outage.

**2. Per-check billing event (Contract 19.1 metering):**

```
Topic: cypherx.guardrails.usage.recorded
Partition key: tenant_id
Payload:
{
  "tenant_id":    "<uuid>",
  "api_key_id":   "<uuid|null>",
  "agent_id":     "<uuid|null>",
  "operation":    "check.input | check.output | simulate",   // dot-notation (standardized); check.both removed — /check/both is dropped from first cycle
  "units":        { "input_bytes": 4096, "output_bytes": 0, "rules_evaluated": 8 },
  "cost_usd":     0.00002,                  // sum of per-rule unit cost (Component 2 rule cost table)
  "duration_ms":  18,
  "request_id":   "<uuid>",
  "trace_id":     "<uuid>"
}
```

Emitted via the same transactional outbox pattern as Phase 3 (so a service crash does not lose a billable check), reached through the Component 4 post-response persist queue — the outbox INSERT happens off the decision path, with the same never-dropped overflow semantics (synchronous fallback when the queue is full). Sampling is NEVER applied — every billable check produces an event.

Per-rule unit cost is added to `guardrails.rules.estimated_cost_usd_per_call` and ranges from 0.000001 (cheap regex) to 0.005 (Llama Guard 2 classifier inference). Documented in `contracts/billing/guardrails-rule-cost.md` (artifact authored in WP01 by the contracts pass; the table MUST match `guardrails.rules.estimated_cost_usd_per_call` row-for-row).

---

### Component 6 — Policy Simulation ⚡ (PROMOTED)

> **Promoted to first-cycle** because external customers cannot safely author policies they can't test. Without simulation, every policy change is a direct production change. Simulation is non-negotiable for external SaaS.

**What it is:** Test a policy (real or draft) against sample text without enforcing it. Does not write to `violations` and does not emit `violation.detected` events.

```
POST /v1/policies/{policy_id}/simulate
Body: {
  "input_text": "Sample text to test",
  "direction":  "input | output",
  "as_of":      "2026-05-24T00:00:00Z"   // optional — simulate against a historical policy version
}

Response: same as /check/input but with extra fields:
{
  "simulated":  true,
  "would_decision": "allow | block | redact | warn",
  "would_violations": [ ... ],
  "would_redacted_text": "..." ,
  "evaluation_trace": [ {"rule": "...", "decision": "...", "duration_ms": 1} ]
}

Limit:   100 simulations/hour for free, 1000/hour for pro, unlimited for enterprise.
Billing: emits cypherx.guardrails.usage.recorded with operation="simulate" but cost_usd=0
         (platform absorbs simulation cost as a UX cost-of-doing-business).
```

**Draft simulation (no persisted policy yet):**

```
POST /v1/policies/simulate
Body: {
  "policy_definition": { ...full policy YAML/JSON inline... },
  "input_text": "...",
  "direction": "input"
}
```

This lets external developers test a policy in the dashboard before saving it. The draft is never persisted.

---

### Component 6b — Policy Management CRUD ⚡ (PROMOTED)

> **Promoted to first cycle (see Amendment Log).** Simulation (Component 6), custom
> rules (Component 8), append-only versioning, the `fail_mode_override` paper trail
> (Component 5b), and agent assignment all hard-depend on tenants being able to
> author the policies the ⚡ surface operates on. Without CRUD, no tenant can create
> what every other promoted feature consumes. **Scope: `tenant:admin` on every
> mutating endpoint.**

**Endpoints (precise semantics):**

| Endpoint | Semantics |
|----------|-----------|
| `POST /v1/policies` | Create a new policy (`version = 1`, `status = 'active'`). Body is the Component 3 policy definition (YAML/JSON). |
| `POST /v1/policies/{policy_id}` | **Append-only edit**: inserts a NEW row with a new `policy_id`, `version = old + 1`, `previous_policy_id = old.policy_id`; old row → `status = 'superseded'`; ALL `agent_policies` rows pointing at the old `policy_id` are repointed **atomically in the same transaction** (the Component 3 invariant). Never mutates `policies.rules` in place. |
| `PUT /v1/agents/{agent_id}/policy` | **Assignment**: upsert into `guardrails.agent_policies` for the caller's tenant. Body `{ "policy_id": "<uuid>" }`. The policy must belong to the caller's tenant (or be the platform default). |
| `GET /v1/policies`, `GET /v1/policies/{policy_id}` | List / fetch (superseded rows included — history is part of the product). |

**Validation at save time (every create/edit):**
- Every `rules[]` entry's `rule_id` must reference an existing `guardrails.rules`
  row (platform or own-tenant, `status = 'active'`) — app-side validation, same
  rule as the registry comment in Component 3.
- `stream_mode` must be `buffer` in first cycle (Component 5c — `window`/`post`
  references are rejected at save, not just at startup).
- `action_override` / `fail_mode_override` values validated against their enums.

**`fail_mode_override` paper trail:** any rule entry that sets
`fail_mode_override` is **audit-logged on save** (who, when, rule, old → new
value) — this is the Component 5b audited-override requirement made concrete.

**Cache visibility:** writes become visible to enforcement within one policy-cache
refresh cycle (≤ 60 s, Component 3).

---

### Component 7 — Async Mode 📋

**What it is:** Request is allowed immediately; guardrails check runs asynchronously. Used for non-blocking audit workflows.

```
POST /v1/check/input   with  { "mode": "async" }
Response: { "check_id": "<uuid>", "status": "queued" }

Client can poll: GET /v1/checks/{check_id}
Or receive webhook callback when check is complete.
```

---

### Component 8 — Custom Tenant Rules ⚡ (PROMOTED)

> **Promoted to first-cycle** because external customers building their own products need to express their own safety/compliance rules from day one. Platform-default rules cover the common cases (PII, toxicity, prompt-injection) but not domain-specific ones (HIPAA-PHI templates, financial-disclosure, brand safety). A guardrails SaaS that only ships hardcoded rules is not a SaaS — it's a fixture.

**What it is:** Tenants can add custom rules on top of platform defaults.

```
POST /v1/policies/{policy_id}/rules
Body: {
  "type":     "regex | classifier-threshold",             // FIRST CYCLE. "semantic" is 📋, gated on WP06 embeddings; "chain" is DELETED (was undefined)
  "name":     "Block competitor mentions",
  "pattern":  "(?i)(rival-company|competitor-brand)",     // for type=regex
  "examples": [ "...sample 1...", "...sample 2..." ],     // 📋 for type=semantic, once WP06 embeddings land
  "action":   "block | redact | warn",
  "category": "business | pii | toxicity | jailbreak | custom",
  "stream_mode": "buffer",
  "fail_mode_override": null
}

POST /v1/rules                Standalone custom rule (reusable across policies)
GET  /v1/rules                List custom rules for this tenant + platform-default rules
PUT  /v1/rules/{rule_id}      Update custom rule (creates a new version row; old version retained)
DELETE /v1/rules/{rule_id}    Soft-delete (status='retired'); policies pinned to old version keep working
```

**Data model adjustment:** `guardrails.rules` gains a `tenant_id UUID` column (nullable; NULL = platform rule, NOT NULL = tenant rule). RLS: `USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id')::uuid)`. The mixed-scope read pattern is the same as `llms.model_aliases`.

**Validation pipeline at write time:**
1. Regex compiled and tested against a 64 KiB random input (rejected if execution > 50ms — ReDoS guard).
2. 📋 (gated on WP06 embeddings) Semantic rules require 3+ examples; embedded via LLMs gateway (cost charged against `guardrails.checks_per_min`). In first cycle, `type=semantic` is rejected 422 at save.
3. CI lint via `POST /v1/policies/simulate` against `contracts/guardrails/golden-suite.jsonl` (>=55 known-input/known-decision pairs, growing — see `contracts/guardrails/golden-suite.jsonl`; artifact authored in WP01 by the contracts pass, seeded from existing test fixtures) — rule MUST NOT flip decisions on the golden suite vs platform-default baseline. Tenants can opt out of golden-suite gating via tenant:admin acknowledgement.

**Quota (per Contract 19):** `custom_rules_max` per tenant. Breach → 422 `QUOTA_EXCEEDED`.

**Per-tenant rule cost:** custom rules charge `guardrails.usage.recorded` at the same per-rule rate as platform rules. 📋 Semantic-similarity rules are billed for the embedding cost (forwarded to LLMs usage event) — lands with the WP06-gated semantic type.

---

### K8s Deployment Spec (DEPLOY-TARGET / cloud form — conditional on the infra phase; first cycle runs the Compose-Parity Runtime below)

```yaml
Namespace:   shared-core
Deployment:  guardrails-service
Replicas:    min 2, max 10 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: core

Resources:
  requests: { cpu: 500m, memory: 768Mi }
  limits:   { cpu: 2000m, memory: 2Gi }
  # Memory bumped allowance for detoxify model (~250 MB resident) + Atlas runtime + buffers.

Startup probe (CRITICAL — model load takes 10–30s; without this, K8s kills the pod):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 30          # 150s total grace period

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / classifier / Valkey / Kafka.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - PostgreSQL reachable
    #   - At least one active platform-default policy exists (resolution chain step 3)
    #   - detoxify model loaded in process memory
    # Soft deps (log + metric only, do NOT fail readiness):
    #   - Valkey (policy cache misses fall through to DB)
    #   - Kafka (outbox keeps events durable until publisher reconnects)

Env vars (compose: env_file/.env; cloud form: Doppler):
  DATABASE_URL                                 (PgBouncer → guardrails schema, runtime user grd_user)
  VALKEY_URL                                    (policy cache, soft)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_SERVICE_URL                              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET                      (Contract 12; from service-auth/guardrails-service/bootstrap_secret)
  REDACTION_HMAC_KEY_PLATFORM                   (Component 5; standardized name — replaces REDACTION_HMAC_KEY)
  REDACTION_KEK                                 (Component 5; KEK for sealed:v1 tenant redaction keys)
  CLASSIFIER_MODE                               (stub | detoxify — Component 2 image split)
  CLASSIFIER_THRESHOLD                          (Component 2; classification threshold, env-driven)
  PERSIST_QUEUE_MAX                             (Component 4 post-response persist queue bound)
```

> **JWKS verification** follows the same pattern as Phase 3 LLMs gateway: in-cluster
> URL only (never via ALB), 5-min cache, refresh-on-`kid`-miss rate-limited to 1/min.

### Compose-Parity Runtime (first cycle — the actual build/run target)

First cycle builds and verifies against the compose stack (compose + Neon +
Valkey + Redpanda + MinIO — **no AWS / K8s / Kong / px0**). The K8s spec above is
the cloud form of each item below:

- **Images:** the compose `prod` profile runs `guardrails:ml` (baked detoxify
  checkpoint, `CLASSIFIER_MODE=detoxify`, eager load before readiness); `dev`/CI
  runs `guardrails:slim` (`CLASSIFIER_MODE=stub`).
- **Probes:** compose `healthcheck` on `/readyz` with `start_period: 150s` — the
  stand-in for the K8s startupProbe (same model-load grace; same hard/soft dep
  rules as the readinessProbe comments above).
- **Secrets/config:** every env var above arrives via compose `env_file`/`.env`;
  Doppler is the cloud form. Nothing is fetched from AWS — tenant redaction keys
  use the `sealed:v1:` / `env:` backends (Component 5).
- **Kafka topics:** created by the idempotent `topics-init` compose job
  (`rpk topic create ...`) — the Terragrunt stand-in.
- **No Kong:** `X-Request-ID` is injected by the callers (xAgent / BFF); the trace
  middleware's UUID-validate + synthesize fallback (Component 4) covers the rest.

---

## ⚡ First Cycle Implementation Checklist

- [ ] Service architecture planned separately (rule engine internals, classifier packaging)
- [ ] `POST /v1/check/input` synchronous endpoint — **service JWT + X-Forwarded-Agent-JWT; identity rejected in body**; response `duration_ms` (not `latency_ms`)
- [ ] `POST /v1/check/output` synchronous endpoint (buffer mode only) — same auth pattern; accepts `text` + optional `input_text` (for `output-pii-email-v1` context); **does NOT accept `system_prompt`** (rules that need it are 📋 embedded-library mode)
- [ ] **`violations[].matched` in API response is redacted/truncated to the same rule as DB storage** (PII → `[REDACTED:...]` token; non-PII → ≤64-char truncation). CI test gates both write paths.
- [ ] 6 first-cycle INPUT rules (prompt-injection, pii-email, pii-phone, pii-credit-card, toxicity, jailbreak)
- [ ] **5 first-cycle OUTPUT rules** (output-pii-email, output-pii-credit-card, output-toxicity, output-jailbreak-leak — **regex/pattern-only, no system-prompt comparison**, output-max-length); pattern list for output-jailbreak-leak published in `contracts/guardrails/jailbreak-leak-patterns.md` (artifact authored in WP01 by the contracts pass)
- [ ] **detoxify packaged + loaded in-process (Component 2 packaging story)** — `guardrails:slim` (stub, dev/CI) + `guardrails:ml` (torch-cpu, checkpoint **baked at build with pinned sha256**, no first-use download); **EAGER load in lifespan before readiness**; `CLASSIFIER_MODE` / `CLASSIFIER_THRESHOLD` env-driven; compose prod profile runs `:ml` (no separate inference pod for first cycle)
- [ ] **`guardrails.rules` registry table created + seeded with the 11 first-cycle rule rows**; per-rule `params` JSONB seeded (e.g. `output-max-length-v1` → `{"max_chars": ...}`)
- [ ] **Latency SLO instrumented + alerted as CACHE-HIT SLO measured at decision-return** (`/check/input` p99 ≤ 50ms; `/check/output` p99 ≤ 100ms on the policy-cache-hit path; cold-path allowance p99 ≤ 250ms under `cache_hit="false"` label with its own sustained-cold-path alert; persistence is post-response and outside the budget)
- [ ] Platform default policy defined and seeded — exactly one row enforced by partial unique index
- [ ] **Policy resolution chain implemented** (`agent_policies → tenant default → platform default`); `/readyz` 503 if step 3 missing
- [ ] **Append-only policy versioning** (`previous_policy_id`, `status` transitions); historical reconstruction via `violations.policy_id`
- [ ] **Policy management CRUD (Component 6b) ⚡ (PROMOTED)** — `POST /v1/policies` (create), `POST /v1/policies/{id}` (append-only edit: new row, version+1, `previous_policy_id`, atomic `agent_policies` repoint), `PUT /v1/agents/{agent_id}/policy` (assignment); rule-reference + `stream_mode` validation at save; audit-logged `fail_mode_override`; scope `tenant:admin`
- [ ] Block, redact, warn modes implemented — check endpoints are **200-always** (`decision` in body); the **caller mints the 422 GUARDRAIL_VIOLATION**
- [ ] **Deterministic PII redaction `[REDACTED:<category>:<HMAC-token>]`** with `REDACTION_HMAC_KEY_PLATFORM` (env-supplied; Doppler is the cloud form) + per-tenant override via `guardrails.tenant_redaction_keys` on the pluggable secret-backend registry (`sealed:v1:` / `env:` first cycle; `secretsmanager:` + per-tenant KMS CMK = future cloud backend; `POST /v1/redaction-keys/rotate` for tenant:admin)
- [ ] **Per-tenant rate limit (Component 5d)** — `checks_per_min`, `input_bytes_per_min`, `custom_rules_max` from `auth.tenant_quotas` (Contract 19); Valkey sliding window; fail-closed on outage; 429 with `X-RateLimit-*` headers
- [ ] **Per-check billing event** — `cypherx.guardrails.usage.recorded` on every check, via outbox; carries `units`, `cost_usd`, `duration_ms`, never sampled
- [ ] **Policy Simulation (Component 6) ⚡** — `POST /v1/policies/{id}/simulate` (against persisted policy) + `POST /v1/policies/simulate` (against draft); rate-limited, never persists violations
- [ ] **Custom tenant rules (Component 8) ⚡** — types `regex` + `classifier-threshold` only (`semantic` 📋 gated on WP06 embeddings; `chain` deleted); `guardrails.rules.tenant_id` column with mixed-scope RLS; `POST /v1/rules`, `PUT`, `DELETE`; ReDoS guard at write time; golden-suite (`contracts/guardrails/golden-suite.jsonl`, authored in WP01) CI gate opt-out for tenant:admin
- [ ] **All rules default `fail_mode: closed`**; per-policy override paper-trailed; plan-tier-based defaulting removed
- [ ] **Streaming-output strategy: `buffer` only for first cycle** — startup error if a policy references `window` or `post`
- [ ] Violation log written on every violation — **PK is UUID; `direction` column (renamed from `check_type`); `policy_id` FK (`rule_id` app-side validated, FK dropped); `request_id` NOT NULL (= inbound `X-Request-ID`, validated as UUID); `trace_id` NOT NULL (parsed from `traceparent`); fallback-synth+WARN (`request_id_generated_fallback=true`) if either header is absent OR `X-Request-ID` is non-UUID — rows ALWAYS written; CI junk-header test**
- [ ] **`matched_text` rule enforced** (redaction token for PII categories, ≤64-char truncation for others); CI test gates merges
- [ ] **Post-response persist queue (Component 4)** — bounded buffer (`PERSIST_QUEUE_MAX`), `guardrails_persist_backlog` gauge, overflow → synchronous inline fallback (never sampled, never dropped), drained on shutdown
- [ ] **Outbox pattern** (`guardrails.outbox`) — violation + outbox row in one transaction (written by the persist-queue drainer); publisher loop with DLQ after 10 attempts
- [ ] **Kafka event payload** aligned with Contract 5 (`direction`, `policy_id`, `trace_id`, `request_id` present); partition key = `tenant_id`
- [ ] Policy assignment per agent (with fallback to platform default)
- [ ] Policy cache in Valkey (60s TTL) — **MANDATORY on the hot path (cache-hit SLO precondition)**; cache miss falls through to DB at cold-path latency; Valkey outage does NOT fail readiness
- [ ] Atlas migrations (Contract 14) for `guardrails.*` schema (rules, policies, agent_policies, violations, outbox)
- [ ] RLS policies applied: tenant-scoped (`violations`, `agent_policies`); mixed-scope (`policies`); platform-scoped (`rules`)
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints; readiness gated on Postgres + platform default policy + classifier loaded
- [ ] **Model-load grace configured** — compose `healthcheck` with `start_period: 150s` so eager model load doesn't trip health (cloud form: K8s startupProbe, 30 × 5s)
- [ ] Runs in the compose stack — `guardrails:ml` under the prod profile, `guardrails:slim` for dev/CI; topics via the `topics-init` job (cloud form: K8s via ArgoCD — see Deploy-target checklist)

### Deploy-target checklist (conditional on the infra phase — cloud forms of the items above)

- [ ] Deployed to K8s via ArgoCD (namespace `shared-core`, HPA min 2 / max 10)
- [ ] K8s startupProbe (150s grace) + liveness/readiness probes per Contract 7
- [ ] Env vars sourced from Doppler (incl. `REDACTION_HMAC_KEY_PLATFORM`)
- [ ] `secretsmanager:` backend for tenant redaction keys (AWS Secrets Manager + per-tenant KMS CMK)
- [ ] Kafka topics provisioned via Terragrunt (compose stand-in: `topics-init` job)

## 📋 Full Enterprise Implementation Checklist

- [ ] All remaining rules (SSN, passport, name NER, hallucination, format validation) — output-PII rules are ⚡ (promoted; duplicate removed)
- [ ] Custom rule type `semantic` (3+ examples, embedded via LLMs gateway) — gated on WP06 embeddings pre-work
- [ ] Async check mode with status polling
- [ ] Topic blocklist (semantic similarity-based; gated on WP06 embeddings)
- [ ] Policy inheritance (org-level → agent-level override)
- [ ] Hot reload (policy changes take effect without pod restart)
- [ ] Audit mode (allow + silent log)
- [ ] Violation trend dashboard data (aggregate stats API)
- [ ] Watermarking hooks for AI-generated content detection
- [ ] Llama Guard 2 / Prompt Guard split out as separate inference deployment
- [ ] Streaming output: `window` and `post` modes (requires gRPC streaming or embedded library)
- [ ] xAgent embedded-library mode for guardrails (avoid 2× HTTP RTT per task)
- [ ] xAgent circuit breaker (`cypherx.guardrails.breaker.tripped` Kafka event)
- [ ] `REDACTION_HMAC_KEY_PLATFORM` quarterly rotation runbook

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Synchronous Guardrails Dependency Risk — REAL
Evidence: lines 24–26, 45–69, 202–209. Every LLM call routes through `/v1/check/input` and `/v1/check/output`; Component 5b (524–553) defines fail modes only at the service level.
**Mitigation:** xAgent implements circuit-breaker on consecutive 5xx; fail-closed default with audited per-policy override.

### 2. Rule Execution Ordering Undefined — REAL
Evidence: lines 54–62. Pipeline says "where possible" parallel; no deterministic order or short-circuit rules.
**Mitigation:** rules execute in lexicographic order by `rule_id`; first BLOCK halts evaluation; otherwise aggregate violations and apply BLOCK > REDACT > WARN > ALLOW precedence.

### 3. ML Classifier Bottleneck Risk — PARTIAL
Evidence: lines 195–200 (in-process detoxify, single-threaded RoBERTa).
**Mitigation:** model is per-pod (not shared); throughput ceiling = replicas × per-pod QPS = 1 / (model latency + rule overhead). Move classifier to a separate inference pod once it exceeds 30 % of total check latency.

### 4. Custom Tenant Regex Complexity Risk — REAL
Evidence: lines 673–708. ReDoS guard is one line.
**Mitigation:** at rule-creation, compile regex with RE2-style NFA→DFA (no backtracking engine); reject patterns failing a 64 KiB random-input test or exceeding 50 ms; forbid unbounded repetition / nested quantifiers; monthly CI scan of all tenant rules.

### 5. Future Policy Explosion Complexity — NOT-REAL
Evidence: lines 278–309. `guardrails.policies` is tenant-scoped; `guardrails.rules` is shared platform registry. RLS isolates. Linear in rules; intentional in policies.

### 6. Runtime Enforcement vs Policy Management Separation — PARTIAL
Evidence: lines 231–332. Same service handles policy mutations and enforcement.
**Mitigation:** policies cached in Valkey 60 s TTL (line 789); changes visible within one refresh cycle. Pod restart acceptable for rule-definition changes in first cycle; future: split policy-management sidecar + watch-based cache invalidation.

### 7. Tail Latency Amplification Risk — REAL
Evidence: lines 54, 208–209. xAgent pays 2× guardrails latency (pre+post); sequential eval amplifies tail.
**Mitigation:** carry `estimated_latency_p99_ms` per rule in `guardrails.rules`; evaluate independent rules in parallel; total p99 = sqrt(sum branches p99²) + serial overhead. Targets: input p99 ≤50 ms, output p99 ≤100 ms.

### 8. Degradation Strategy for Expensive Rules Missing — REAL
Evidence: lines 195–209. No per-rule timeout or fallback chain.
**Mitigation:** each rule carries `timeout_ms` (regex 10 ms, ML 50 ms). On timeout: log WARN, increment `rule_evaluations_timeout`, apply `default_fail_mode`. Document per-rule fallback chain (heuristic regex as fallback for ML).

### 9. High-Concurrency Throughput Sensitivity — REAL
Evidence: lines 716–722. Resources set but no QPS target.
**Mitigation:** first-cycle target = 10–50 QPS (single-tenant agent concurrency); HPA cap 10 replicas ≈ 500 req/s aggregate. Load-test must validate p99 ≤100 ms at 50 req/s per pod before production.

### 10. Future Compiled Policy Runtime Need — NOT-REAL
Evidence: lines 211–216, 809–811. WASM / compiled DSL already tracked as future follow-up.
