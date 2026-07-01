# Phase 8 — Skills System
> **Status:** ⏳ Pending | **Depends On:** Phase 5 (RAG), Phase 7 (Tool Registry) | **Blocks:** Phase 9 (enhanced)
> **First Cycle:** 📋 Not required for first cycle. Required before Phase 9 enhanced pass.

---

## Phase Overview

Skills are **declarative workflow definitions** — YAML/JSON files that tell an agent *how* to approach a task. They are not code. A skill defines what tools to use, what steps to follow, what constraints to apply, and what success looks like. Agents discover skills via semantic search through the RAG service.

**Deliverable:** A skills repository with schema-validated skill definitions indexed into RAG, plus a `tool-skill-retriever` MCP server that agents use to discover the right skill for a task.

> 🏗️ **Service Architecture Note:** The `tool-skill-retriever` MCP server is a lightweight service; its architecture should be planned before implementation. The skills repo itself is a Git repo with CI validation, not a running service.

---

## High Level Design

### System Context

```
Skills Repo (Git)
  │ on push/PR merge
  ▼
CI Pipeline (GitHub Actions)
  │ validate schema → index into RAG
  ▼
RAG Knowledge Base: "skills-kb"
  │ semantic indexed
  ▼
tool-skill-retriever (MCP Server)
  │ exposes skill search to agents
  ▼
xAgent
  1. Query tool-skill-retriever: "find skill for task: summarise a research document"
  2. Receive: top-k matching skills with YAML definition
  3. Select best fit skill
  4. Execute skill steps using tool calls + LLM calls
```

### Skill Retrieval Flow

```
xAgent receives task description
  │
  ▼
POST /mcp/invoke (tool-skill-retriever)
  input: { "task_description": "...", "top_k": 3 }
  │
  ▼
tool-skill-retriever calls RAG:
  POST /v1/knowledge-bases/skills-kb/query
  { "query": task_description, "top_k": 3 }
  │
  ▼
RAG returns top matching skill YAML chunks
  │
  ▼
tool-skill-retriever parses + returns full skill definitions
  │
  ▼
xAgent selects best skill and executes its steps
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after first cycle.

---

### Component 1 — Skill Schema ⚡

The canonical schema is **Contract 11 (Phase 0 post-edit)**: `input_schema` and `output_schema` are JSON Schema draft 2020-12; steps use `action_type` + `action_ref` (no free-form action strings).

> **Service-endpoint registry (`contracts/services/endpoints.yaml`) — REQUIRED for `action_type: service`:**
> `action_type=service` action_refs use the form `"<service>.<endpoint>"`
> (e.g., `"rag.query"`, `"memory.retrieve"`, `"guardrails.check_input"`). To make
> CI validation possible (and to give skill authors a single document to read),
> the platform maintains a hand-edited registry of every callable service endpoint:
>
> ```
> contracts/services/endpoints.yaml
> ───────────────────────────────────
> services:
>   rag:
>     endpoints:
>       query:
>         http:        POST /v1/knowledge-bases/{kb_id}/query
>         input_schema:  $ref: ../api/rag-query-request.schema.json
>         output_schema: $ref: ../api/rag-query-response.schema.json
>         required_scopes: [internal:read]
>         status: active
>       finalize:
>         http:        POST /v1/knowledge-bases/{kb_id}/ingest/finalize
>         ...
>   memory:    { endpoints: { store: {...}, retrieve: {...}, ... } }
>   guardrails: { endpoints: { check_input: {...}, check_output: {...} } }
>   llms:      { endpoints: { embeddings: {...} } }
> ```
>
> This file lives in the platform contracts repo and is updated by the team that
> owns each service when they ship a new endpoint. CI for Phase 8 reads it on
> every skill validation run.

```yaml
# Example skill: Research and Summarise
skill_id:     "com.cypherx.research.research-and-summarise"
version:      "1.0.0"
name:         "Research and Summarise"
description:  "Search the web for information on a topic and produce a structured summary"
status:       published
tags:         [research, summarisation, web, information-gathering]

required_capabilities:                # NEW — preferred form (Phase 7 Component 1c)
  - web-search                        # resolved per-tenant: defaults to platform tool, tenant may override
required_tools:                       # DEPRECATED — kept for migration; still accepted
  - tool-web-search@1.0.0             # version-pinned; falls back if capability not bound
required_services:
  - llms
optional_services:
  - memory

input_schema:                       # JSON Schema draft 2020-12 (Contract 11 post-edit)
  type: object
  required: [topic]
  properties:
    topic:
      type: string
      description: "Topic to research"
    depth:
      type: string
      enum: [shallow, deep]
      default: shallow
    max_sources:
      type: integer
      default: 5
      minimum: 1
      maximum: 20

output_schema:                      # JSON Schema draft 2020-12
  type: object
  properties:
    summary:
      type: string
    sources:
      type: array
      items:
        type: object
        properties:
          title: { type: string }
          url:   { type: string, format: uri }

steps:
  - id: search
    action_type: capability                          # enum: capability | tool | llm | service (NEW: capability is preferred)
    action_ref:  "web-search"                        # capability name — resolver picks the bound tool@version per-tenant
    input:
      query:       "{{input.topic}}"
      max_results: "{{input.max_sources}}"
    output_var: search_results

  # Legacy form still accepted (for backwards-compat with existing skills):
  # - id: search
  #   action_type: tool
  #   action_ref:  "tool-web-search.web_search"
  #   input: { query: "{{input.topic}}", max_results: "{{input.max_sources}}" }
  #   output_var: search_results

  - id: summarise
    action_type: llm
    action_ref:  "smart"                             # model alias from LLMs Gateway model_aliases
    input:
      messages:
        - role: system
          content: "You write concise, source-attributed research summaries."
        - role: user
          content: |
            Based on the following search results about "{{input.topic}}", write a
            {{input.depth}} summary. Format with key points and source attribution.

            Search results:
            {{search_results.results | format_results}}
    output_var: final_summary

  # Example of action_type: service (used by foundational skills like answer-with-context):
  - id: rag_lookup
    action_type: service
    action_ref:  "rag.query"                         # "<service>.<endpoint>" from contracts/services/endpoints.yaml
    input:
      kb_id:   "{{input.kb_id}}"
      query:   "{{input.topic}}"
      top_k:   5
      min_score: 0.7
    output_var: rag_results
    on_error: skip                                   # skill continues even if RAG is empty/down

constraints:
  max_tokens:      2000
  timeout_seconds: 60
  max_cost_usd:    0.10

guardrails:                          # METADATA ONLY in first cycle — see note below
  input:  [default-platform-policy]
  output: [default-platform-policy]
```

> **`guardrails:` is metadata only for first cycle. Runtime does NOT honor it.**
> Phase 4's policy resolution chain is strictly hierarchical: `agent_policies →
> tenant default → platform default`. There is no name-based policy lookup API,
> so the runtime cannot say "for this skill, use the policy named X". Adding such
> an API is a real surface change with cross-tenant authorisation questions (can
> a skill in tenant A reference a policy in tenant B? what about a tenant-local
> policy that shadows the platform default mid-skill?) — too much new surface for
> first cycle.
>
> What the field DOES in first cycle:
> - Documents which policy the skill author tested against.
> - Surfaces in PR review so reviewers can sanity-check policy alignment.
> - Future-proofs the schema for the 📋 follow-up that wires runtime enforcement.
>
> What it does NOT do in first cycle:
> - It does NOT cause xAgent to switch policies for this skill's LLM calls.
> - It does NOT override the agent's assigned `policies.policy_id`.
> - Conflicts between this field and the runtime-resolved policy are silently
>   ignored (the runtime policy wins, always).
>
> CI validates that referenced names exist in any tenant's `guardrails.policies`
> at index time — purely an author-helps check, not enforcement.

> **`action_type` + `action_ref`** replaces the prior `action: tool:<x> | llm:chat | service:<y>` string. Parseable, validatable, no regex. The CI schema validator (Component 3) rejects skills using the legacy form.

> **`required_services` vs `optional_services` enforcement (MANDATORY — xAgent owns the check):**
> - `required_services` is enforced at **skill-load time, BEFORE step 0 runs**. xAgent
>   calls `GET /livez` against each listed service (cluster DNS, 2s deadline per service,
>   checked in parallel). Any failure → reject the skill invocation with error code
>   `SKILL_DEPENDENCY_UNAVAILABLE` and the failing service in the error body. NO partial
>   execution. This makes "is this skill runnable right now?" a single readiness query,
>   not a half-finished-skill mystery.
> - `optional_services` is **documentation only** — runtime does NOT check. Skill steps
>   referencing them must handle absence gracefully (typically via `on_error: skip`).
> - Service names use the same string set as `action_type=service` action_refs
>   (`llms`, `memory`, `rag`, `guardrails`, …). CI cross-checks that every service named
>   in `required_services` / `optional_services` exists in the service-endpoint registry
>   (`contracts/services/endpoints.yaml`, see Component 3).

> **Step failure semantics (MANDATORY — xAgent's executor contract):**
> Each step accepts two optional fields on top of `id`/`action_type`/`action_ref`/`input`/`output_var`:
>
> ```yaml
> steps:
>   - id: search
>     action_type: tool
>     action_ref: "tool-web-search.web_search"
>     input: { ... }
>     output_var: search_results
>     on_error:   abort                 # abort | retry | skip   (default: abort)
>     retry:                            # only honored when on_error: retry
>       max_attempts:    3              # int, 1-5
>       backoff_seconds: 2              # int, exponential (2, 4, 8, ...)
> ```
>
> - **`abort` (default):** skill exits immediately with `SKILL_STEP_FAILED`,
>   carrying the failing `step.id`, the error from the underlying tool/llm/service,
>   and a snapshot of rendered inputs / partial outputs. No downstream steps run.
> - **`retry`:** xAgent re-executes the same step up to `max_attempts` times with
>   exponential backoff. The retry budget includes the original attempt. After
>   exhaustion, behaves as `abort`. Idempotency-Key for the underlying tool call
>   is held constant across retries so providers can dedup (Phase 3/5/6 pattern).
> - **`skip`:** the step is marked `status=skipped` in `task_steps`. Any downstream
>   `{{<this-step>.output.*}}` reference resolves to `null` and the template engine
>   renders it as empty string. NO automatic null-propagation beyond template
>   rendering — downstream `{% if ... %}` blocks SHOULD guard against the skipped
>   path. CI flags any direct downstream dependency on a `skip`-marked step's
>   output with a warning (not an error — the skill author may know what they're doing).
>
> No `continue-on-error` flag. No `on_error: <expression>`. The three modes above
> cover every first-cycle pattern; richer error semantics are 📋.
>
> Every step's `status` (`success | skipped | failed | timed_out`), `duration_ms`,
> `attempts`, and rendered input/output snapshots land in xAgent's `task_steps`
> (Phase 9). The skill-runner does NOT silently swallow errors.

---

### Component 2 — Skills Repository Structure ⚡

```
Skills/ (Git repo or directory in cypherx-platform monorepo)
│
├── registry/
│   ├── index.yaml          ← CI-GENERATED on every merge to main; manual edits rejected
│   └── categories.yaml     ← Skill category definitions (hand-edited; small enum)
│
├── definitions/
│   ├── research/
│   │   ├── research-and-summarise/
│   │   │   ├── v1.0.0.yaml
│   │   │   └── CHANGELOG.md
│   │   └── web-research-deep/
│   │       └── v1.0.0.yaml
│   ├── content-generation/
│   ├── code/
│   ├── data-extraction/
│   ├── workflow-automation/
│   ├── customer-support/
│   ├── data-analysis/
│   └── multi-agent/
│
└── tests/
    ├── skill-schema-validator.py   ← Validates all YAML files against Contract 11
    └── mock-tool-runner.py         ← Tests skill steps against mock tools
```

> **`registry/index.yaml` is CI-generated** — the source of truth is the YAML files
> in `definitions/`. CI walks `definitions/`, emits a deterministically-sorted index,
> and fails the PR if a non-CI commit modified the file. Manual edits drift away
> from the source of truth and get silently overwritten on the next CI run.

---

### Component 3 — Skill CI/CD Pipeline ⚡

```yaml
# .github/workflows/skills-index.yml
# Triggered on push to Skills/ directory

Steps:
  1. Schema validation:
     - Parse each YAML file in definitions/ to JSON.
     - ajv validate against Contract 11 JSON Schema (draft 2020-12).
     - Validate input_schema and output_schema are themselves valid JSON Schema.
     - For each step:
         * action_type ∈ {tool, llm, service}.
         * action_type=tool → action_ref matches "<existing-tool>.<existing-function>"
           in Tool Registry; tool version must be active or deprecated (NOT removed).
         * action_type=llm → action_ref resolves in LLMs Gateway model_aliases or
           is a literal model id (warn on literal: prefer alias).
         * action_type=service → action_ref matches "<service>.<known-endpoint>"
           in `contracts/services/endpoints.yaml` (platform-maintained registry —
           see Component 1 service-endpoint note); endpoint's `status` must be
           `active` or `deprecated` (NOT `removed`).
     - `required_services` / `optional_services` — every entry MUST exist in
       `contracts/services/endpoints.yaml` top-level service list. Unknown → fail.
     - **Templates — TWO checks (both required):**
         * (a) Helper allow-list — every `{{ x | helper }}` helper must be in the
           platform-provided allow-list (Component 4b). Unknown helpers → fail.
         * (b) Variable-path resolution — every `{{ var.path... }}` reference must
           resolve. `var` is either:
             - `input` → path must be a valid JSON-pointer into the skill's
               `input_schema`. (Required fields are guaranteed; optional fields
               warn if used without an `if defined` guard.)
             - `<step_id>.output` → step_id must exist; path must be a valid
               JSON-pointer into the referenced step's effective output_schema:
                 * tool step: pulled from Tool Registry manifest.
                 * llm step: the unified LLMs response schema (Phase 3 Component 1).
                 * service step: the endpoint's response schema from
                   `contracts/services/endpoints.yaml`.
             - `<step_id>` (no `.output`) → equivalent to `<step_id>.output` for
               legacy ergonomics; same validation.
           Unresolvable references → CI fail with file path + line + the unresolved
           expression. This is the single highest-value validator — a typo here
           silently renders to "" at runtime and the LLM prompt becomes garbage.

  2. Determine diff vs main:
     ADDED   = files present in HEAD, not in main
     MODIFIED= files present in both, content differs
     DELETED = files present in main, not in HEAD
     DEPRECATED = files where status field flipped published → deprecated (subset of MODIFIED)

  3. RAG synchronisation (deterministic doc_id = sha256(skill_id + "@" + version)):
     For each ADDED or MODIFIED file:
       skill = parse_yaml(file)
       doc_id = sha256(skill.skill_id + "@" + skill.version)
       # Idempotent re-ingest: delete-then-create (RAG document update is 📋, Phase 5).
       DELETE /v1/knowledge-bases/{platform_skills_kb_id}/documents/{doc_id}   # 404 ok
       POST   /v1/knowledge-bases/{platform_skills_kb_id}/ingest/inline
              { "name": skill.skill_id + "@" + skill.version,
                "content": serialise(skill),
                "source_type": "markdown",
                "doc_id": doc_id,                       # client-supplied for determinism
                "metadata": { "skill_id": ..., "version": ..., "tags": ..., "status": ... } }

     For each DELETED file OR DEPRECATED skill:
       doc_id = sha256(skill_id + "@" + version)
       DELETE /v1/knowledge-bases/{platform_skills_kb_id}/documents/{doc_id}

     Skill YAMLs are small (typically ≤ 10 KiB); ingest/inline cap (100 KiB per
     Phase 5) is comfortable headroom.

  4. Regenerate registry/index.yaml (CI-generated; manual edits rejected):
     - Walk definitions/, build a flat list of {skill_id, version, status, name, tags, path}.
     - Sort deterministically; emit YAML.
     - If `git diff registry/index.yaml` shows hand-edits in the PR (a non-CI commit
       touched the file), CI fails the PR with "index.yaml is CI-generated; remove
       manual edits".

  5. CI auth (GitHub Actions runs OUTSIDE the cluster — Doppler operator is NOT reachable):
     - Pipeline assumes Phase 1 Component 18's GitHubActionsRole via OIDC
       (sts:AssumeRoleWithWebIdentity). NO long-lived AWS access keys in GH.
     - The assumed role grants `secretsmanager:GetSecretValue` on
       `arn:aws:secretsmanager:<region>:<acct>:secret:cypherx/ci/*` only
       (scoped IAM — see Phase 1 Component 18 cross-phase update below).
     - CI fetches the skills-CI bootstrap secret at
       `cypherx/ci/skills-ci/bootstrap_secret` (per-env, KMS-encrypted).
     - That bootstrap secret POSTs to Auth `/v1/service-tokens` and receives a
       5-minute service JWT for `skills-ci`.
     - The service JWT carries `on_behalf_of = <platform-skills-writer agent_id>`
       (well-known agent under the platform tenant; see Component 4).
     - RAG ingest calls go through Kong over HTTPS (CI is outside the cluster).
     - WHY NOT Doppler: Doppler's operator syncs secrets to K8s Secret → env var,
       which only works for in-cluster pods. CI in GH Actions has no Doppler
       runtime hook unless we ship a long-lived Doppler API token in GH Secrets —
       precisely the long-lived-credential pattern Phase 1 went OIDC to avoid.
       Doppler stays the right tool for `tool-skill-retriever` (in-cluster).
```

---

### Component 4 — Skills Knowledge Base (RAG) ⚡

```
Knowledge Base: platform-skills            (matches Phase 5 Component 10 bootstrap)
  tenant_id:         00000000-0000-0000-0000-000000000001    (platform tenant — Contract 13)
  chunking_strategy: sentence
  embedding_model:   embed (resolved to text-embedding-3-small / 1536 dims at creation, immutable)

Resolution:
  Skill-retriever and CI MUST resolve kb_id by NAME at startup:
    GET /v1/knowledge-bases?tenant_id=00000000-0000-0000-0000-000000000001&name=platform-skills
  Cache the resolved kb_id for the process lifetime. NO hardcoded UUIDs.
```

> **No RAG RLS exception.** Earlier draft introduced a "cross-tenant READ allow-rule
> on the platform-tenant KB" — that would have created a special-case RLS bypass
> at the RAG layer, breaking the Contract 13 promise that "cross-tenant data access
> is architecturally impossible". Phase 5 Component 10 already committed to the
> alternative (option (b)): callers switch RLS context to the platform tenant when
> querying `platform-skills`. Phase 8 implements that path here, NOT a RAG exception.

**Access path (no exceptions, all-tenant-isolation preserved):**

1. **`platform-skills-reader` agent** — created at platform bootstrap (idempotent), under tenant `00000000-...-001`, with scopes `[rag:query]`. This is a real `auth.agents` row, not a wildcard.

2. **`platform-skills-writer` agent** — created at platform bootstrap, under the same tenant, with scopes `[rag:query, rag:write]`. Used only by the skills-ci pipeline service.

3. **Skill-retriever** (Component 5), on receiving an xAgent invocation:
   - Mints (or fetches from in-process cache) a long-lived **agent JWT for `platform-skills-reader`** via Auth `/v1/agents/{platform-skills-reader-id}/token`. Token TTL 1h, refreshed every 50 min in background.
   - Calls RAG with:
     - `Authorization: Bearer <skill-retriever's service JWT>` (Contract 12)
     - `X-Forwarded-Agent-JWT: <platform-skills-reader's agent JWT>` ← tenant context = platform
   - RAG sets `app.tenant_id = platform_uuid`, RLS gates the query naturally, query runs against `platform-skills`. **No RAG-layer exception, no audit special-case.**

4. **Skills-CI pipeline** (Component 3) uses the same pattern with the `platform-skills-writer` agent JWT.

5. The xAgent's original tenant context is preserved through trace headers and `X-Original-Agent-JWT` (informational only — RAG ignores it for the query but the audit pipeline correlates skill discovery back to the calling tenant).

> **Auth bootstrap dependency:** Phase 2 Atlas migration must create the two well-known agents on platform bootstrap (idempotent INSERT ON CONFLICT). Their agent_ids are fixed UUIDs from the well-known registry (Phase 0 Contract 13):
>   - `platform-skills-reader` agent_id: `00000000-0000-0000-0000-0000000000a1`
>   - `platform-skills-writer` agent_id: `00000000-0000-0000-0000-0000000000a2`
> Phase 8's migration adds these rows; Phase 0 well-known registry docs them.

---

### Component 4c — Tenant-Private Skills KB ⚡ (NEW)

For external operability, every tenant gets its own private skills KB in addition to reading platform skills.

**Bootstrap on tenant creation:**

When `cypherx.tenant.created` fires (Contract 13 lifecycle event), the skills-ci service's tenant-bootstrap consumer creates a RAG knowledge base:

```
Knowledge Base: tenant-skills-{tenant_id}
  tenant_id:         <the new tenant's UUID>
  chunking_strategy: sentence
  embedding_model:   embed (inherits from tenant_config)
  visibility:        private (only this tenant can read/write)
```

**Tenant skill submission flow (external publishers):**

```
POST /v1/skills/submit
Authorization: Bearer <api-key-jwt with scope skills:publish>
Body: {
  "skill_yaml": "<full YAML body>",
  "publish_to": "tenant | review | marketplace"
}

Flow:
  1. Validate skill_yaml against Contract 11 JSON Schema.
  2. Run CI checks (Component 3): schema, template, action_ref resolution
     against caller's resolved capability bindings (Phase 7 Component 1c).
  3. Ingest into tenant-skills-{tenant_id} KB under tenant's RLS context.
  4. publish_to: tenant      → status='published', visible to tenant only.
              : review       → status='review', cross-tenant marketplace queue;
                               platform admin reviews. On approval, copied to
                               platform-skills (visibility='marketplace').
              : marketplace  → alias for 'review' (kept for SDK ergonomics).

Returns: { "skill_id": "...", "version": "...", "status": "published|review" }
```

**Skill retrieval merges platform + tenant skills:**

The `tool-skill-retriever`'s `find_skills` tool issues TWO RAG queries:
1. Platform-skills (using `platform-skills-reader` agent JWT — as today).
2. tenant-skills-{caller_tenant_id} (using a per-tenant reader agent created at bootstrap, `00000000-0000-0000-0000-0000000001{tenant_suffix}` for low-collision; or, cleaner, mint a request-scoped JWT for `tenant-skills-reader` template agent under the caller's tenant).

Results merged with reciprocal-rank fusion (RRF), tenant skills boosted by a small constant (caller authored these; they probably want them surfaced first).

---

### Component 4b — Template Engine for Skill Steps ⚡ (NEW)

Skill steps reference values with `{{...}}` syntax (`{{input.topic}}`, `{{search.output.results | format_results}}`). The template language must be one specific implementation, not "Mustache-or-Jinja-or-whatever".

**Choice: Pongo2 / Jinja2-compatible.** Specifically a Go/Python pair that share semantics so the skill executor can be implemented in either language without skill rewrites.

**Supported features:**
- Variable expansion: `{{input.field}}`, `{{step_id.output.field}}`, `{{step_id.output.array | length}}`.
- Filters / helpers — PLATFORM-PROVIDED, hardcoded allow-list ONLY:
  - `length`, `join(sep)`, `truncate(n)`, `jsonify`, `lower`, `upper`, `default(value)`
  - `format_results` — renders search/tool results into a markdown bullet list
  - `format_sources` — renders a numbered source list
  - Skill authors CANNOT define new helpers. CI rejects templates that reference any unknown helper name. This is non-negotiable — skill-local helpers would be code injection (anyone with PR rights could define `eval`).
- Conditionals (limited): `{% if input.depth == "deep" %}...{% endif %}`.
- No loops, no template inheritance, no I/O — strictly side-effect-free string templating.

**Sandbox:**
- Template execution time-limited to **100ms per render**.
- On render timeout: step fails with error code `TEMPLATE_TIMEOUT`; skill execution aborts (no partial run). The failing step is recorded in `task_steps` with the rendered prefix (≤ 1 KiB) and the error.
- Template output truncated at **64 KiB** before being passed to LLM/tool.
- Templates are NOT user input — they are skill-author input committed in Git and reviewed in PR.

> **Intermediate-content guardrails (deliberate first-cycle trade-off):**
> Tool output that flows into an LLM step's prompt (via `{{search.output.results | format_results}}`) is NOT passed through guardrails between steps in first cycle. xAgent's post-LLM guardrail check still catches dangerous LLM output, but a malicious web-search result containing prompt-injection text reaches the LLM unfiltered. Accept-and-document for first cycle; 📋 follow-up adds a per-step `guardrail_intermediate: true` flag that runs `/check/input` on the rendered prompt before the LLM call.

---

### Component 5 — tool-skill-retriever MCP Server ⚡

**What it is:** An MCP server that gives agents the ability to find relevant skills. Follows the Phase 7 MCP standard verbatim — no special-case shape.

**MCP manifest (Contract 4 post-edit shape; NO `auth_required` field):**
```json
{
  "schema_version":   "1.0.0",
  "protocol_version": "mcp/1.0",
  "name":             "tool-skill-retriever",
  "display_name":     "Skill Retriever",
  "version":          "1.0.0",
  "description":      "Search the platform skill library for skills relevant to a task",
  "author":           "CypherX Platform",
  "category":         "platform",
  "tags":             ["skills", "discovery", "platform"],
  "required_scopes":  ["tool:invoke", "tool:tool-skill-retriever:invoke"],

  "tools": [
    {
      "name":        "find_skills",
      "description": "Find relevant skills for a task description",
      "input_schema": {
        "type": "object",
        "required": ["task_description"],
        "properties": {
          "task_description": { "type": "string", "description": "Describe what you want to accomplish" },
          "top_k":             { "type": "integer", "default": 3, "minimum": 1, "maximum": 10 },
          "tags":              { "type": "array", "items": { "type": "string" } }
        }
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "skills": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "skill_id":    { "type": "string" },
                "name":        { "type": "string" },
                "description": { "type": "string" },
                "version":     { "type": "string" },
                "score":       { "type": "number" },
                "definition":  { "type": "object" }
              }
            }
          },
          "duration_ms": { "type": "integer" }
        }
      },
      "timeout_seconds":    30,
      "idempotent":         true,
      "estimated_cost_usd": 0.0001,
      "rate_limit":         { "rpm": 120, "rpd": 50000 }
    }
  ],

  "health_endpoint":  "/livez",
  "metrics_endpoint": "/metrics"
}
```

**Implementation (Phase 7 standard auth + dispatch — no `auth_required`, no `/authorize` per-call):**
```
POST /mcp/v1/invoke
Headers:
  Authorization:         Bearer <xagent service-jwt>     ← Contract 12
  X-Forwarded-Agent-JWT: <calling agent's JWT>          ← preserves audit context
  traceparent:           00-<trace-id>-<span-id>-01     ← Contract 8
  Idempotency-Key:       <uuid>                         ← honored (find_skills is idempotent)
Body: { "tool": "find_skills", "input": { "task_description": "...", "top_k": 3 } }
  │
  ▼
1. Verify service JWT locally via JWKS (5-min cache).
2. Verify X-Forwarded-Agent-JWT locally via JWKS; extract caller's agent_id, tenant_id (audit context).
3. Verify BOTH scopes in calling JWT: tool:invoke AND tool:tool-skill-retriever:invoke. Missing either → 403.
4. Validate input against the tool's input_schema; 422 VALIDATION_ERROR on failure.
5. Switch to platform-skills-reader identity (Component 4):
   - Fetch the cached platform-skills-reader agent JWT (refreshed every 50 min in background).
   - Resolve platform_skills_kb_id by name (cached at startup; Component 4).
6. Call RAG:
   POST /v1/knowledge-bases/{platform_skills_kb_id}/query
   Headers:
     Authorization:         Bearer <skill-retriever's service-jwt>
     X-Forwarded-Agent-JWT: <platform-skills-reader's agent JWT>    ← tenant_id = platform
     traceparent:           <propagated>
   Body: { "query": task_description, "top_k": top_k * 2, "filters": { "tags": ... } }
7. For each returned chunk:
   - Attempt YAML parse.
   - If parse fails: log WARN, increment metric skill_retriever_yaml_parse_errors_total{reason}, SKIP the chunk.
8. Deduplicate (same skill_id, keep highest score).
9. Return top_k parsed skill definitions with scores + duration_ms.

Endpoints (Phase 7 standard — every tool the same):
  POST /mcp/v1/invoke
  GET  /manifest                                     ← unversioned per Phase 7 post-edit
  GET  /livez                                        ← process-only liveness
  GET  /readyz                                       ← Hard: RAG reachable + platform_skills_kb_id resolved + platform-skills-reader JWT loaded
  GET  /metrics                                      ← histogram (NOT quantile-label summary)
```

---

### Component 6 — Foundational Skills Set ⚡

**First cycle: 5 foundational skills to author and index:**

| Skill ID | Description | Tools Used |
|----------|-------------|-----------|
| `research-and-summarise` | Research a topic and produce summary | tool-web-search |
| `answer-with-context` | Answer a question using knowledge base | rag (directly) |
| `structured-data-extraction` | Extract data fields from unstructured text | llm only |
| `document-qa` | Answer questions about an uploaded document | rag |
| `step-by-step-problem-solving` | Decompose and solve a complex problem | llm only |

**📋 Full enterprise skill categories (author 30+ skills across):**
- Research & Information Gathering (5 skills)
- Content Generation: writing, summarisation, translation (5 skills)
- Code Generation & Review (5 skills)
- Data Processing & Analysis (5 skills)
- Workflow Automation (3 skills)
- Customer Support Flows (3 skills)
- Data Extraction (3 skills)
- Multi-agent coordination patterns (4 skills)

---

### Component 7 — Skill Lifecycle Management

**⚡ First cycle (schema + CI enforcement only):**
- Schema CHECK on `status ∈ {draft, review, published, deprecated}` (rejects unknown values at validate time).
- CI removes deprecated skills from RAG (Component 3, step 3 DELETED+DEPRECATED branch).

**📋 Post-first-cycle (state-machine enforcement, automation):**
- State-machine enforcement: only legal transitions (`draft → review → published → deprecated`); reverse transitions require platform-admin override.
- PR comment automation describing the transition.
- CHANGELOG auto-generated from PR metadata.
- Sunset notice published in `registry/index.yaml` (`sunset_at` field).
- Community contribution flow (post-SDK).

```
Lifecycle states: draft → review → published → deprecated

State transitions (📋 enforcement):
  draft → review:        PR opened in skills repo
  review → published:    PR merged (CI validates + indexes)
  published → deprecated: CHANGELOG updated, status field changed,
                         CI removes from RAG index, adds sunset notice
```

---

### K8s Deployment (tool-skill-retriever)

```yaml
Namespace:   tools
Deployment:  tool-skill-retriever-v1-0-0           # version-pinned per Phase 7 post-edit
Service:     tool-skill-retriever-v1-0-0           # cluster DNS resolved by Tool Registry
Replicas:    min 2, max 4 (HPA on CPU 60% — first-cycle minimum)
Node selector: node-role: tools

Resources:
  requests: { cpu: 200m, memory: 256Mi }
  limits:   { cpu: 1000m, memory: 512Mi }
  # Bumped from 100m/128Mi/500m/256Mi: YAML parse + RAG round-trip + JSON marshal
  # easily exceeds 256Mi under modest concurrency.

Startup probe (must resolve platform_skills_kb_id + load reader JWT before serving):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12          # 60s grace

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches RAG / Auth / Valkey.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - RAG /livez reachable (cluster DNS)
    #   - platform_skills_kb_id resolved (by name; cached)
    #   - platform-skills-reader agent JWT loaded (cached, refreshed every 50 min)
    # Soft deps (log + metric only):
    #   - Valkey (idempotency cache; missing → cache-miss-only behaviour)

Env vars (from Doppler):
  AUTH_SERVICE_URL              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET      (Contract 12; from service-auth/tool-skill-retriever/bootstrap_secret)
  RAG_SERVICE_URL               (http://rag-service.shared-core.svc.cluster.local:8080)
  PLATFORM_SKILLS_KB_NAME       ("platform-skills" — name, NOT a UUID; resolved at startup)
  PLATFORM_TENANT_UUID          (00000000-0000-0000-0000-000000000001)
  PLATFORM_SKILLS_READER_AGENT_ID (00000000-0000-0000-0000-0000000000a1; well-known)
  VALKEY_URL                    (soft — idempotency cache)
```

> **JWKS verification** follows the Phase 3 pattern: in-cluster URL only, 5-min cache,
> refresh-on-`kid`-miss rate-limited to 1/min.

> **Service ACL (cross-phase update — Phase 8 migration extends `auth.service_acl`):**
> - `xagent → tool-skill-retriever [internal:write]`
> - `tool-skill-retriever → rag-service [internal:read]`
> - `tool-skill-retriever → auth-service [internal:read]` (JWKS + service-token mint + agent-token mint for platform-skills-reader)
> - `tool-registry → tool-skill-retriever [internal:read]` (manifest poll + health)
> - `skills-ci → rag-service [internal:read, internal:write]` (CI ingest)
> - `skills-ci → auth-service [internal:read]`
>
> Phase 8 ships idempotent `INSERT ... ON CONFLICT DO NOTHING` against `auth.service_acl`.

> **Tool Registry seed migration (cross-phase update — Phase 8 extends Phase 7's seed file):**
> ```sql
> INSERT INTO registry.tools (name, version, is_latest, display_name, description, category,
>                             endpoint_url, rate_limit_rpm, status)
> VALUES
>   ('tool-skill-retriever', '1.0.0', true, 'Skill Retriever',
>    'Search the platform skill library for skills relevant to a task', 'platform',
>    'http://tool-skill-retriever-v1-0-0.tools.svc.cluster.local:8080', 120, 'active')
> ON CONFLICT (name, version) DO NOTHING;
> ```

> **Migration ownership (Phase 8 has no local schema but contributes THREE cross-service migrations):**
>
> Phase 8 owns no `skills.*` PostgreSQL schema — the skills repo is in Git, the
> retriever is stateless, RAG holds the indexed content. But the checklist requires
> seed rows in tables owned by other phases. To keep these migrations reviewable
> and reversible:
>
> ```
> platform-migrations/phase-08/
>   ├── 20260601_0900__seed_platform_skills_agents.sql       → auth.agents
>   │     (the two well-known agents: reader ...00a1, writer ...00a2)
>   ├── 20260601_0901__seed_skills_service_acl.sql           → auth.service_acl
>   │     (the 6 ACL rows listed in this Component)
>   ├── 20260601_0902__seed_skill_retriever_tool.sql         → registry.tools
>   │     (the tool-skill-retriever row above)
>   └── README.md                                            → explains the directory
> ```
>
> Runtime:
> - Applied as Atlas migrations under the platform-admin DDL credential
>   (same pattern Phase 5/6 already use for `auth.service_acl` writes).
> - All three are idempotent (`INSERT ... ON CONFLICT DO NOTHING`).
> - Run as a one-shot K8s Job during Phase 8 deploy (`helm.sh/hook: pre-install`),
>   NOT bundled into Phase 2/7's own migration history (which would re-couple them).
>
> Review:
> - CODEOWNERS for `platform-migrations/phase-08/` requires approval from BOTH
>   the Auth-service owner AND the Tool-Registry owner — cross-service writes do
>   not merge under one team's review.
> - CI runs the migrations against a real Postgres in integration tests; rollback
>   for these specific migrations is `DELETE WHERE ...` of the exact seed rows
>   (the well-known agent_ids and ACL tuples are knowable constants).
>
> Why a separate directory and not in Phase 2/7's repo: keeps the "what does Phase 8
> install" question answerable in one place, and makes uninstall straightforward
> (Phase 8 deprecated = delete these three migrations' effects + remove the
> retriever Deployment).

---

## ⚡ First Cycle Implementation Checklist

- [ ] Skill schema finalised (Contract 11 post-edit — JSON Schema draft 2020-12, `action_type` + `action_ref`)
- [ ] **Step `on_error` contract** — three modes (abort | retry | skip), retry bounded with exponential backoff, Idempotency-Key held constant across retries, every step's status/duration/attempts land in `task_steps`
- [ ] **`required_services` enforced at skill-load time** by xAgent — 2s `/livez` parallel check, reject with `SKILL_DEPENDENCY_UNAVAILABLE` before step 0; `optional_services` is documentation only
- [ ] **Skill `guardrails:` is metadata only** — runtime ALWAYS uses Phase 4's standard policy resolution chain; CI validates referenced names exist
- [ ] **`contracts/services/endpoints.yaml`** maintained as the platform-wide registry of callable service endpoints (rag, memory, guardrails, llms, ...); each entry pins input/output schema + required scopes + status
- [ ] Skills repo directory structure created; `registry/index.yaml` flagged CI-generated only
- [ ] CI schema validator (ajv against Contract 11), tool-reference check against Tool Registry, service-reference check against `endpoints.yaml`, **template helper allow-list check**, **template variable-path resolution check** (unresolved `{{var.path}}` → CI fail)
- [ ] CI handles ADDED / MODIFIED / DELETED / DEPRECATED skills (RAG delete-then-create with deterministic `doc_id = sha256(skill_id + "@" + version)`)
- [ ] CI uses RAG `/ingest/inline` endpoint (≤ 100 KiB cap from Phase 5) with `doc_id` for determinism
- [ ] CI regenerates `index.yaml`; rejects PRs with manual edits to it
- [ ] **CI auth via GitHub OIDC → GitHubActionsRole → Secrets Manager** `cypherx/ci/skills-ci/bootstrap_secret` (NOT Doppler — Doppler is in-cluster only); bootstrap secret POSTs to Auth `/v1/service-tokens` for a 5-min service JWT with `on_behalf_of = platform-skills-writer`
- [ ] **`platform-skills` knowledge base** bootstrapped by Phase 5 Component 10 (NOT recreated here); skill-retriever resolves kb_id by NAME at startup — no hardcoded UUID
- [ ] **`platform-skills-reader` agent** (`...00a1`) and **`platform-skills-writer` agent** (`...00a2`) seeded by Phase 8 Atlas migration into `auth.agents` under the platform tenant, idempotent
- [ ] **`platform-skills-reader` agent JWT cache** in skill-retriever (1h TTL, refresh every 50 min)
- [ ] **NO RAG-layer cross-tenant exception** — Component 4 implements Phase 5 option (b), not the prior allow-rule
- [ ] 5 foundational skills authored and indexed (Component 6)
- [ ] `tool-skill-retriever` architecture planned separately
- [ ] `tool-skill-retriever` MCP server follows Phase 7 standard verbatim:
      - Manifest matches Contract 4 post-edit shape (NO `auth_required`)
      - Endpoints: `POST /mcp/v1/invoke`, `GET /manifest`, `GET /livez`, `GET /readyz`, `GET /metrics`
      - Auth: service JWT + `X-Forwarded-Agent-JWT`; identity rejected in body
      - Both scopes verified (`tool:invoke` AND `tool:tool-skill-retriever:invoke`)
      - Server-side input_schema validation; 422 VALIDATION_ERROR on failure
      - `Idempotency-Key` honored (find_skills is idempotent)
      - Prometheus histogram metrics (NOT quantile-label summary)
      - Response field `duration_ms` (not `latency_ms`)
      - **YAML parse failures in returned chunks SKIPPED with warn + metric**, not error
- [ ] **Versioned K8s Deployment/Service** (`tool-skill-retriever-v1-0-0`); resources bumped (200m/256Mi req, 1000m/512Mi lim)
- [ ] **Startup probe** (60s grace) — gates on `platform_skills_kb_id` resolved + reader JWT loaded
- [ ] **Template engine** (Component 4b) with hardcoded helper allow-list; `TEMPLATE_TIMEOUT` failure mode; 64 KiB output cap; 100 ms render budget
- [ ] **Intermediate-content guardrail trade-off documented** (not implemented in first cycle)
- [ ] **`platform-migrations/phase-08/` directory** holds the 3 cross-service migrations (well-known agents, service ACL rows, tool registry row); CODEOWNERS requires Auth + Tool-Registry approval; runs as pre-install K8s Job under platform-admin DDL credential; all idempotent
- [ ] **Service ACL migration** seeds 6 ACL rows (xagent↔retriever/registry, retriever↔rag/auth, registry↔retriever, skills-ci↔rag/auth)
- [ ] **Tool Registry seed migration** adds `tool-skill-retriever` row (idempotent ON CONFLICT)
- [ ] Deployed to K8s (tools namespace) via ArgoCD
- [ ] **Tenant-private skills KB (Component 4c) ⚡** — bootstrap-tenant consumer creates `tenant-skills-{tenant_id}` KB on `cypherx.tenant.created`; per-tenant `tenant-skills-reader` agent template; merged-retrieval (RRF) of platform + tenant skills
- [ ] **External skill submission (Component 4c)** — `POST /v1/skills/submit` with `publish_to: tenant | review | marketplace`; CI checks include `required_capabilities` resolution against tenant capability bindings
- [ ] **`required_capabilities` resolution at runtime** — preferred over `required_tools`; runtime resolver calls `GET /v1/capabilities/{cap}/binding` for caller's tenant; falls back to legacy `required_tools` for migration period
- [ ] **`action_type: capability`** supported in skill steps; CI validates that every referenced capability exists in `registry.capabilities`
- [ ] **Per-skill billing event** — `cypherx.skills.invoked` ⚡ PROMOTED — via outbox on every skill execution; carries `skill_id`, `version`, `publisher_tenant_id`, `consumer_tenant_id`, `steps_executed`, `total_cost_usd`
- [ ] **Per-tenant skill quota** — `private_skills_max`, `executions_per_min` from `auth.tenant_quotas` (Contract 19)

## 📋 Full Enterprise Implementation Checklist

- [ ] Full 30+ skill library authored
- [ ] Skill validation harness (run skill against mock tools)
- [ ] Skill lifecycle state-machine enforced in CI (Component 7 📋 portion)
- [ ] Skill versioning with backwards compatibility notes
- [ ] Skill CHANGELOG auto-generated
- [ ] Community contribution process (post-SDK)
- [ ] Skill deprecation: auto-remove from RAG + sunset notice in registry
- [ ] Skill search with tag filters
- [ ] Skill usage analytics — `cypherx.skills.invoked` Kafka event via outbox
- [ ] Per-step `guardrail_intermediate: true` flag (intermediate-content guardrails)
- [ ] RAG document update endpoint (replaces delete-then-create idiom in CI Step 3)

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Skills Are Still Static DAGs — REAL
Evidence: lines 9, 156–170. Linear YAML steps; only `abort | retry | skip`.
**Mitigation:** dynamic branching, conditional routing, loop constructs explicitly deferred (📋). First cycle ships linear DAG only.

### 2. Skill YAML Will Become Very Hard to Maintain at Scale — REAL
Evidence: lines 469–505. N tenants × M skill versions = raw YAML; no DSL.
**Mitigation:** skill composition + template library + compiler-assisted generation tracked 📋 post-first-cycle.

### 3. RAG-Based Skill Discovery Alone Will Eventually Become Weak — REAL
Evidence: lines 44–62, 626–636. Discovery is RAG-only.
**Mitigation:** multi-strategy routing (tag exact-match + hybrid + intent-classifier fallback) tracked 📋. First cycle relies on RAG semantic search alone.

### 4. Hidden Prompt Injection Surface — REAL
Evidence: lines 109–115, 177–182, 489 (tenant-supplied YAML embedded in LLM prompts); 539 documents the trade-off.
**Mitigation:** inter-step guardrail enforcement on tool/skill output before LLM injection tracked 📋. First cycle accepts unfiltered intermediate content with documented trade-off.

### 5. Runtime Skill Execution Ownership Slightly Blurred — PARTIAL
Evidence: lines 34–39, 235–284, 622, 847.
**Mitigation:** ownership clarified — xAgent's skill executor calls `GET /v1/capabilities/{cap}/binding`; the retriever only discovers, never resolves capabilities.

### 6. Tenant Capability Resolution Performance Hotspot — REAL
Evidence: lines 117–125, 847. Resolution per invocation, uncached.
**Mitigation:** cache `GET /v1/capabilities/{cap}/binding` per-tenant 5 min; track 📋.

### 7. Missing Explicit State Persistence Model for Long-Running Skills — REAL
Evidence: lines 249–284 (timeout 60 s; only abort/retry/skip).
**Mitigation:** checkpoint step outputs + resume + multi-session orchestration tracked 📋 post-first-cycle.

### 8. Service Dependency Versioning Not Fully Solved — PARTIAL
Evidence: lines 110–124, 245. `required_services` resolved by name only.
**Mitigation:** skills pin `required_services` endpoint versions (e.g., `rag.query@v2`); CI generates per-skill lockfile. Tracked 📋.
