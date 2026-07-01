# Phase 7 — Tools (MCP Servers)
> **Status:** ⏳ Pending | **Depends On:** Phase 0, 1, 2 | **Blocks:** Phase 8, 9 (enhanced)
> **First Cycle:** 📋 Not required for very first cycle. Tool Registry + web-search needed before Phase 9 enhanced.

## Amendment Log (2026-06 — pre-build reconciliation)

- **`registry.tenant_tools` RLS split into per-command policies (Component 1).** The single `FOR ALL`, USING-only `p_tenant_tools_isolation` policy let any tenant UPDATE/DELETE other tenants' marketplace tool rows (supply-chain attack: repoint `endpoint_url`), because the marketplace OR-clause applied to writes and there was no `WITH CHECK`. Now: the `FOR SELECT` policy keeps the marketplace-visibility OR-clause for cross-tenant discovery; `FOR INSERT`/`UPDATE`/`DELETE` policies are tenant-only with both `USING` and `WITH CHECK`.
- **`registry.tool_capabilities` gets its OWN RLS policy (Component 1c).** The "RLS on tenant tool_capabilities is inherited via the tools row" claim was false — RLS does not propagate across joins; the table had NO policy at all. It now carries its own tenant policy (USING + WITH CHECK), with platform rows (`tenant_id IS NULL`) globally readable.
- **Per-invocation metering event re-homed to xAgent's outbox.** Tool servers are stateless (no database), so "billing event via outbox" had no outbox to live in. `cypherx.tools.invocation.metered` is emitted from **`xagent.outbox`** — xAgent records every tool call in `task_steps` and is the only first-cycle caller. Payload: `tool`, `version`, `agent_id`, `tenant_id`, `duration_ms`, `output_bytes`, `status`, `request_id` — units + correlation id ONLY, no cross-schema joins to `llms` pricing (billing joins happen downstream in the usage pipeline, per the Contract 14 single-owner rule). The `publisher_tenant_id`/`consumer_tenant_id` revenue-share fields move to 📋 (marketplace wave). The topic is also listed in phase-09's `xagent.outbox` topic list.
- **System Context diagram reconciled with the spec body (minor batch).** `POST /mcp/invoke` → `POST /mcp/v1/invoke`; `GET /health` → `GET /livez` + `GET /readyz` (Contract 7). No semantic change — the spec body (Component 2) was already correct.
- **Compose-parity (deployment section + checklist split).** First-cycle runtime is compose + Neon + Valkey + Redpanda + MinIO — no K8s/Kong/Istio/Doppler/AWS/Argo/gVisor. A Compose-Parity subsection documents the runtime equivalents; the K8s/Istio specs are relabeled deploy-target (cloud form, conditional on the infra phase); the ⚡ checklist is split into "service code" (compose-buildable) vs "deploy-target". `S3_TOOLS_OUTPUT_BUCKET`/`S3_ENDPOINT`/`S3_SSE_MODE ∈ {none, kms}` are fully env-driven (MinIO + `none` first cycle; SSE-KMS is the cloud form); an idempotent `topics-init` compose job (`rpk topic create`) stands in for Terragrunt-provisioned topics.

---

## Phase Overview

Tools are independently deployable servers that agents can invoke. Each tool encapsulates a specific capability (web search, code execution, HTTP requests, file operations) and exposes a standard interface.

This phase also builds the **Tool Registry** — the central catalogue agents query to discover available tools and their current health.

**Deliverable:** Tool Registry service + `tool-web-search` (first cycle). All other tool servers built as full enterprise implementation.

> 🏗️ **Service Architecture Note:** The internal architecture of each individual tool server (sandboxing implementation, execution runtime, tool-specific retry logic) must be planned separately per tool before implementation begins. The Tool Registry architecture must also be planned separately.

### Important: Protocol Naming Clarification ⚡

The platform uses the term "MCP" (Model Context Protocol) to describe the standard interface each tool server implements. To avoid confusion with Anthropic's MCP specification:

- **CypherX MCP (this phase's protocol)** is an **HTTP+JSON RPC-style protocol** over standard REST: `POST /mcp/v1/invoke`, `GET /manifest`, `GET /livez` + `GET /readyz`, `GET /metrics` (paths reconciled with Component 2 — see Amendment Log). It is intentionally simpler than Anthropic's MCP and lives inside the cluster behind Istio mTLS (cloud form; see Compose-Parity subsection for the first-cycle equivalent).
- **Anthropic MCP** is JSON-RPC 2.0 over stdio or SSE, designed for Claude Desktop, IDE plugins, and similar long-lived single-tenant transports. It is not compatible with multi-tenant cluster-internal routing without an adapter.
- **A bridge** (`tool-mcp-bridge`, 📋 Phase 13 or later) will speak Anthropic MCP outward so external Claude Desktop / IDE clients can invoke CypherX tools.
- If a third-party Anthropic-MCP tool server needs to be onboarded, a small adapter pod translates between the two protocols.

The internal protocol is named "CypherX MCP" in code (`CMCP`) and "MCP" colloquially. The manifest schema (Contract 4) borrows shape from Anthropic's manifest where reasonable to keep future bridge work small.

---

## High Level Design

### System Context

```
                    ┌─────────────────────────────────────────────┐
                    │            TOOL REGISTRY                     │
                    │                                             │
  xAgent ──────────►│  GET /v1/tools          (discover)         │
  Admin ────────────│  POST /v1/tools/register (register tool)   │
  Kong ─────────────│  GET /v1/tools/{name}/health               │
                    └──────────────┬──────────────────────────────┘
                                   │ registry knows about ▼
              ┌────────────────────┼────────────────────────────────┐
              ▼                    ▼                                 ▼
    MCP: tool-web-search   MCP: tool-code-exec            MCP: tool-http-client
    (ns: tools)            (ns: tools, gVisor)             (ns: tools)

Each MCP Server:
  POST /mcp/v1/invoke     ← xAgent calls this to execute a tool
  GET  /manifest          ← Registry calls this to read capabilities
  GET  /livez + /readyz
  GET  /metrics
```

### MCP Invocation Flow

```
xAgent needs web search
  │
  ▼
1. Query Tool Registry: GET /v1/tools?category=search&name=tool-web-search
   → Returns version-specific endpoint:
     { name: "tool-web-search", version: "1.2.0",
       endpoint: "http://tool-web-search-v1-2-0.tools.svc.cluster.local:8080",
       healthy: true, manifest_etag: "..." }
  │
  ▼
2. xAgent calls MCP server:
   POST http://tool-web-search-v1-2-0.tools.svc.cluster.local:8080/mcp/v1/invoke
   Headers:
     Authorization:         Bearer <service-jwt>          ← Contract 12 service token (xAgent's)
     X-Forwarded-Agent-JWT: <agent-jwt>                   ← agent identity preserved
     traceparent:           00-<trace-id>-<span-id>-01    ← W3C, Contract 8
     Idempotency-Key:       <uuid>                        ← optional, honored if tool is idempotent
   Body (NO identity fields):
     { "tool": "web_search", "input": { "query": "...", "max_results": 5 } }
  │
  ▼
3. MCP server:
   a. Verifies service JWT locally via JWKS; rejects 401 if invalid.
   b. Verifies X-Forwarded-Agent-JWT locally via JWKS; extracts agent_id, tenant_id, scopes.
   c. Checks agent JWT has BOTH scopes (Contract 4 post-edit):
        - tool:invoke                           (coarse)
        - tool:<server-name>:invoke             (fine; e.g. tool:tool-web-search:invoke)
      Missing either → 403 FORBIDDEN.
   d. Validates input against the tool's input_schema; 422 VALIDATION_ERROR on failure
      (details: { "field_path": "/input/query", "reason": "required" }).
   e. Calls Auth /authorize ONLY if the action needs stateful policy evaluation
      (tenant suspended, plan-tier gate, ABAC). Otherwise stay local (Phase 2 layer-A pattern).
   f. Executes the tool.
   g. Returns MCP-formatted result.
  │
  ▼
4. xAgent uses tool result to continue task; records tool call in task_steps.
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after first cycle.

---

### Component 1 — Tool Registry ⚡

**What it is:** Central catalogue of all MCP servers. Agents query it to discover tools.

> **Schema ownership (resolves Contract 14 single-owner rule):** the registry table
> lives in its own schema `registry.*`, NOT in `platform.*`. The platform-mgmt
> service (Phase 11) owns `platform.*`; the Tool Registry service owns `registry.*`.
> Cross-service reads (e.g., platform-mgmt rendering a "tools used" panel) use a
> dedicated read-only DB role; cross-service writes are forbidden.

> **NEW — two coexisting tool spaces.** External operability requires tenants to register their own private tools without seeing or being seen by other tenants:
> - **Platform tools** live in `registry.tools` (no `tenant_id`). They are vetted, gated by `platform:admin`, and visible to all tenants.
> - **Tenant tools** live in `registry.tenant_tools` (`tenant_id NOT NULL` + RLS). They are private to the tenant, gated by `tools:publish` scope on a tenant API key, and visible only to that tenant.
> - The Registry discovery API (`GET /v1/tools`) returns the UNION (platform + caller's tenant tools), tagged with `scope: platform | tenant`. Agents bind tools by `<name>@<version>` regardless of scope; the registry resolves to the correct row by checking tenant first, falling back to platform.

**PostgreSQL (`registry.tools`):**
```sql
CREATE TABLE registry.tools (
  tool_id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  name                 VARCHAR(100) NOT NULL,
  version              VARCHAR(20)  NOT NULL,                   -- semver
  is_latest            BOOLEAN      NOT NULL DEFAULT true,
  display_name         VARCHAR(255) NOT NULL,
  description          TEXT         NOT NULL,
  category             VARCHAR(50)  NOT NULL,
  tags                 TEXT[]       NOT NULL DEFAULT '{}',
  endpoint_url         VARCHAR(500) NOT NULL,                   -- version-specific cluster DNS
  rate_limit_rpm       INTEGER,                                 -- mirrors Contract 4 manifest field
  rate_limit_rpd       INTEGER,
  estimated_cost_usd   NUMERIC(12,8),
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',
                       -- active | degraded | offline | deprecated
  last_health_check_at TIMESTAMPTZ,
  last_health_status   VARCHAR(20),
  manifest_etag        VARCHAR(64),                             -- last fetched manifest ETag
  metadata             JSONB        NOT NULL DEFAULT '{}',
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT version_semver CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$')
);

-- Multiple versions coexist per tool; exactly one row may be is_latest per tool:
CREATE UNIQUE INDEX uq_tools_name_version ON registry.tools(name, version);
CREATE UNIQUE INDEX uq_tools_name_latest  ON registry.tools(name) WHERE is_latest = true;

-- Platform-scoped table — no tenant_id, no RLS. Mutations gated by platform:admin scope
-- (tenant-published tools are 📋).
```

> **`auth_required` column DROPPED.** Every first-cycle tool requires auth — the
> field was a dead flag that invited misconfiguration. Public/unauth tools are
> Phase 13+ territory; re-add only when there's a real use case.

> **`manifest_url` column DROPPED.** By convention `manifest_url = endpoint_url + "/manifest"`.
> Keeping two columns invites drift. Third-party tools with externally-hosted manifests
> are 📋; if added, introduce a typed `manifest_ref` column then, not before.

**API:**
```
GET    /v1/tools                       List tools (UNION platform + tenant)            ⚡
GET    /v1/tools/{name}                Get latest version (tenant-priority resolution) ⚡
GET    /v1/tools/{name}@{version}      Get specific version                            ⚡
GET    /v1/tools/{name}/health         Check tool health                               ⚡
POST   /v1/tools                       Register PLATFORM tool (platform:admin scope)   ⚡
POST   /v1/tenant-tools                Register TENANT tool (tools:publish scope)      ⚡
PUT    /v1/tenant-tools/{name}@{ver}   Update tenant tool                              ⚡
DELETE /v1/tenant-tools/{name}@{ver}   Soft-delete tenant tool                         ⚡
GET    /v1/public/tools                Public marketplace listing (no auth required)   ⚡ (read-only, paginated)
DELETE /v1/tools/{name}@{version}      Deregister a platform version (platform:admin)  📋
```

**`registry.tenant_tools` table:**

```sql
CREATE TABLE registry.tenant_tools (
  tool_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL,
  publisher_tenant_id  UUID NOT NULL,                      -- usually = tenant_id; differs for marketplace tools published BY tenant A and INSTALLED BY tenant B
  name                 VARCHAR(100) NOT NULL,
  version              VARCHAR(20)  NOT NULL,
  is_latest            BOOLEAN      NOT NULL DEFAULT true,
  display_name         VARCHAR(255) NOT NULL,
  description          TEXT,
  category             VARCHAR(50)  NOT NULL,
  tags                 TEXT[]       NOT NULL DEFAULT '{}',
  endpoint_url         VARCHAR(500) NOT NULL,
  visibility           VARCHAR(20)  NOT NULL DEFAULT 'private'
                       CHECK (visibility IN ('private', 'tenant-public', 'marketplace')),
  capability           VARCHAR(50),                         -- e.g. 'web-search', 'http-fetch' — see Component 1c
  manifest_etag        VARCHAR(64),
  status               VARCHAR(20)  NOT NULL DEFAULT 'pending_review'
                       CHECK (status IN ('pending_review', 'active', 'rejected', 'degraded', 'offline', 'deprecated', 'sunset')),
  review_notes         TEXT,
  image_digest         TEXT,                                -- container image sha256 (mandatory for marketplace)
  sandbox_class        VARCHAR(20)  NOT NULL DEFAULT 'gvisor'
                       CHECK (sandbox_class IN ('gvisor', 'firecracker', 'wasm', 'native-trusted')),
  egress_allowlist     TEXT[]       NOT NULL DEFAULT '{}',  -- host/CIDR allow-list for outbound calls
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT version_semver CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$')
);

CREATE UNIQUE INDEX uq_tenant_tools_name_version ON registry.tenant_tools(tenant_id, name, version);
CREATE UNIQUE INDEX uq_tenant_tools_name_latest  ON registry.tenant_tools(tenant_id, name) WHERE is_latest = true;

ALTER TABLE registry.tenant_tools ENABLE ROW LEVEL SECURITY;

-- SPLIT policies (AMENDED 2026-06 — see Amendment Log). The previous single FOR ALL,
-- USING-only policy let any tenant UPDATE/DELETE other tenants' marketplace rows
-- (supply-chain attack: repoint endpoint_url). Reads keep marketplace visibility;
-- writes are tenant-only, gated in BOTH directions (USING + WITH CHECK).
CREATE POLICY p_tenant_tools_select ON registry.tenant_tools
  FOR SELECT
  USING (tenant_id = current_setting('app.tenant_id')::uuid
         OR (visibility = 'marketplace' AND status = 'active'));    -- marketplace visible cross-tenant for discovery

CREATE POLICY p_tenant_tools_insert ON registry.tenant_tools
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE POLICY p_tenant_tools_update ON registry.tenant_tools
  FOR UPDATE
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE POLICY p_tenant_tools_delete ON registry.tenant_tools
  FOR DELETE
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Marketplace install (tenant A installs publisher B's tool)
CREATE TABLE registry.tenant_tool_installs (
  install_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  installer_tenant_id  UUID NOT NULL,
  publisher_tenant_id  UUID NOT NULL,
  tool_name            VARCHAR(100) NOT NULL,
  version              VARCHAR(20)  NOT NULL,
  enabled              BOOLEAN      NOT NULL DEFAULT true,
  installed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE registry.tenant_tool_installs ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant_tool_installs ON registry.tenant_tool_installs
  USING (installer_tenant_id = current_setting('app.tenant_id')::uuid);
```

**External publisher submission flow:**

1. Publisher tenant posts `POST /v1/tenant-tools` with manifest + container image reference. Required scope: `tools:publish`.
2. Registry validates manifest (Contract 4), runs Trivy/Snyk image scan, lints `sandbox_class` (must be `gvisor` unless platform-admin override), runs the manifest's declared egress allowlist through SSRF policy lint.
3. Row inserted with `status='pending_review'` and `visibility='private'`. Publisher can immediately test invocation against their own tenant.
4. To promote to `visibility='tenant-public'` (own tenant) or `marketplace` (cross-tenant), publisher posts `POST /v1/tenant-tools/{name}@{ver}/publish`. Marketplace requests enter a review queue gated by `platform:admin`.
5. Approved marketplace tools become visible in `GET /v1/public/tools`; install/uninstall by other tenants via `POST/DELETE /v1/marketplace/install`.

**Per-tenant tool quota (Contract 19):** `auth.tenant_quotas.tools.private_tools_max`, `tools.publishable_versions_max` enforced at write time. 429 `QUOTA_EXCEEDED`.

> Discovery returns the **version-specific** `endpoint_url` (e.g.,
> `http://tool-web-search-v1-2-0.tools.svc.cluster.local:8080`). An unversioned alias
> Service MAY exist for convenience but the registry NEVER returns it — version pinning
> in `xagent.agents.allowed_tools` ("`tool-web-search@1.2.0`") is honoured end-to-end.

**Platform-bundled tool seeding (ATLAS MIGRATION — required for first cycle):**
```sql
-- registry/db/migrations/*__seed_platform_tools.sql
INSERT INTO registry.tools (name, version, is_latest, display_name, description, category,
                            endpoint_url, rate_limit_rpm, status)
VALUES
  ('tool-web-search', '1.0.0', true, 'Web Search', 'Search the web and return ranked results',
   'research', 'http://tool-web-search-v1-0-0.tools.svc.cluster.local:8080', 60, 'active')
ON CONFLICT (name, version) DO NOTHING;
-- Subsequent platform tools (tool-code-exec, tool-http-client, etc.) appended per phase.
```

**Health check + bootstrap manifest poll:**
```
On registry startup (BEFORE opening the listener):
  - For every active is_latest=true row, eagerly GET endpoint/manifest.
  - Cache manifest body + ETag.
  - If a manifest fetch fails, mark that row status='degraded' but do NOT block startup.
  - /readyz returns 503 until the initial poll completes for ALL latest rows.

Then, every 30s background poll:
  GET {endpoint_url}/livez (NOT /health — Contract 7 post-edit)
  → Update last_health_check_at, last_health_status.
  → If 3 consecutive failures: status = degraded.
  → If 5 consecutive failures: status = offline.
  → Re-fetch /manifest if If-None-Match returns 200 (ETag changed).
  → Cache health status in Valkey: tool-health:{name}@{version} TTL=30s.

Version retention policy (operational hygiene):
  Max 3 concurrent versions per tool (latest + 2 most-recent deprecated).
  Publishing a 4th version flips the oldest to status='deprecated' and schedules
  K8s teardown 30 days later (📋 ops job; the policy lives here so the registry
  can refuse to register a 4th active version without an explicit override flag).
```

---

### Component 1c — Capability Layer (Skill Portability) ⚡ (NEW)

Skills declare `required_capabilities: [web-search, http-fetch]` rather than concrete tool names. Each capability is implemented by one or more tools. Per-tenant capability resolution maps capability → concrete tool@version:

```sql
CREATE TABLE registry.capabilities (
  capability    VARCHAR(50) PRIMARY KEY,
  description   TEXT NOT NULL,
  input_schema  JSONB NOT NULL,           -- JSON Schema draft 2020-12
  output_schema JSONB NOT NULL
);

INSERT INTO registry.capabilities (capability, description, input_schema, output_schema) VALUES
  ('web-search',  'Search the web and return ranked results', '{...}'::jsonb, '{...}'::jsonb),
  ('http-fetch',  'Fetch a URL and return body',              '{...}'::jsonb, '{...}'::jsonb),
  ('code-exec',   'Run sandboxed code',                       '{...}'::jsonb, '{...}'::jsonb),
  ('file-read',   'Read a file from agent workspace',         '{...}'::jsonb, '{...}'::jsonb),
  ('image-gen',   'Generate an image from a prompt',          '{...}'::jsonb, '{...}'::jsonb),
  ('email-send',  'Send a transactional email',               '{...}'::jsonb, '{...}'::jsonb)
ON CONFLICT (capability) DO NOTHING;

CREATE TABLE registry.tool_capabilities (
  tool_scope       VARCHAR(10) NOT NULL CHECK (tool_scope IN ('platform','tenant')),
  tool_id          UUID NOT NULL,
  tenant_id        UUID,                  -- NULL for platform tools
  capability       VARCHAR(50) NOT NULL REFERENCES registry.capabilities(capability),
  PRIMARY KEY (tool_scope, tool_id, capability)
);

-- AMENDED 2026-06 (see Amendment Log): the previous "RLS is inherited via the tools
-- row" claim was false — RLS does not propagate across joins, and this table had NO
-- policy. tool_capabilities carries its OWN tenant policy:
ALTER TABLE registry.tool_capabilities ENABLE ROW LEVEL SECURITY;

CREATE POLICY p_tool_capabilities_select ON registry.tool_capabilities
  FOR SELECT
  USING (tenant_id IS NULL                              -- platform tool rows: globally visible
         OR tenant_id = current_setting('app.tenant_id')::uuid);

CREATE POLICY p_tool_capabilities_insert ON registry.tool_capabilities
  FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE POLICY p_tool_capabilities_update ON registry.tool_capabilities
  FOR UPDATE
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE POLICY p_tool_capabilities_delete ON registry.tool_capabilities
  FOR DELETE
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
-- Platform rows (tenant_id IS NULL) are written only by migrations / the registry
-- service role (table owner — not subject to these policies).

-- Per-tenant default capability binding (resolver)
CREATE TABLE registry.tenant_capability_bindings (
  tenant_id     UUID NOT NULL,
  capability    VARCHAR(50) NOT NULL REFERENCES registry.capabilities(capability),
  tool_name     VARCHAR(100) NOT NULL,
  version       VARCHAR(20) NOT NULL,
  tool_scope    VARCHAR(10) NOT NULL,     -- 'platform' | 'tenant'
  PRIMARY KEY (tenant_id, capability)
);
ALTER TABLE registry.tenant_capability_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tcb ON registry.tenant_capability_bindings
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**Resolver flow:**
1. Skill execution requests `capability=web-search`.
2. Resolver checks `tenant_capability_bindings` for caller's tenant → returns specific tool@version.
3. If not bound, falls back to platform default (the canonical platform tool tagged with that capability).
4. Invokes via `POST /mcp/v1/invoke` as normal.

**Tenant override use case:** external customer ACME has their own `acme-search@2.1.0` tool. They set `tenant_capability_bindings(tenant=ACME, capability=web-search, tool=acme-search, version=2.1.0)`. Every skill that requires `web-search` now uses ACME's tool — no skill rewrite needed.

**API endpoints:**
```
GET /v1/capabilities                                    List all known capabilities
PUT /v1/capabilities/{cap}/binding                      Set per-tenant binding [scope: tools:admin]
GET /v1/capabilities/{cap}/binding                      Get current binding (effective)
```

---

### Component 2 — MCP Standard Interface ⚡

**Every MCP server must implement exactly these endpoints:**

```
POST /mcp/v1/invoke
  Auth headers (REQUIRED):
    Authorization:         Bearer <service-jwt>          ← caller's service token (Contract 12)
    X-Forwarded-Agent-JWT: <agent-jwt>                   ← agent identity preserved (Contract 12)
    traceparent:           00-<trace-id>-<span-id>-01    ← W3C (Contract 8)

  Optional headers:
    Idempotency-Key:       <uuid>                        ← honored iff manifest.idempotent=true

  Request body (NO identity fields — Contract 13 anti-pattern guard):
  {
    "tool":  "web_search",                ← tool function name from manifest
    "input": { "query": "..." }           ← validated server-side against tool input_schema
  }
  If body contains agent_id / tenant_id / task_id / trace_id → 400 BAD_REQUEST.

  Response (success):
  {
    "output":      { "results": [...] },
    "tool":        "web_search",
    "duration_ms": 234,                                    ← renamed from latency_ms
    "trace_id":    "<uuid>"
  }

  Response (large output — > 10 MiB inline cap, MUST use S3-reference pattern):
  {
    "output":      { "ref": "s3://cypherx-tools-output-<env>/<tenant_id>/<doc_id>",
                     "size_bytes": 12345678, "content_type": "text/html",
                     "expires_at": "2026-05-22T11:00:00.000Z" },
    "tool":        "tool-browser",
    "duration_ms": 1822,
    "trace_id":    "<uuid>"
  }

  Response (error — Contract 2 envelope):
  {
    "error": {
      "code":    "TOOL_EXECUTION_FAILED",
      "message": "Search API returned 503",
      "details": { ... },
      "request_id": "<uuid>",
      "trace_id":   "<uuid>",
      "timestamp":  "2026-05-22T10:00:00.000Z"
    }
  }

GET /manifest    → Returns MCP manifest (Contract 4). UNVERSIONED path — the manifest
                   self-describes its own schema_version. Tool Registry honours ETag /
                   If-None-Match for efficient polling.
GET /livez       → Process-only liveness (Contract 7). NEVER touches downstreams.
GET /readyz      → Dependencies healthy. Hard deps: provider/backing API reachable.
GET /metrics     → Prometheus exposition:
  tool_invocations_total{tool, version, status}
  tool_invocation_duration_seconds_bucket{tool, version, le}
  tool_invocation_duration_seconds_sum{tool, version}
  tool_invocation_duration_seconds_count{tool, version}
  tool_errors_total{tool, version, error_code}
```

> **Prometheus metric fix:** changed from `tool_invocation_duration_seconds{tool, quantile}`
> (a *summary* shape, which does not aggregate across pods) to a histogram
> (`_bucket{le}` + `_sum` + `_count`). Grafana uses `histogram_quantile()` over the
> bucket time series. Summaries are an anti-pattern in a multi-replica deployment.

**Scope enforcement (per Contract 4 post-edit — defence in depth):**
- `tool:invoke` — coarse: agent may call ANY tool.
- `tool:<server-name>:invoke` — fine: agent may call THIS specific tool server.
- A tool server MUST verify BOTH scopes are present in `X-Forwarded-Agent-JWT.scopes` before executing. Missing either → 403 FORBIDDEN. This prevents a compromised xAgent token with only the coarse scope from invoking every tool.

**Input validation (server-side, mandatory):**
- Tools validate `input` against their manifest `input_schema` (JSON Schema draft 2020-12) on every invocation. Defence in depth — xAgent SHOULD also validate, but the tool is the last line.
- Validation failure returns `422 VALIDATION_ERROR` (Contract 2) with `details` carrying the JSON Pointer of the failing field:
  ```json
  { "error": { "code": "VALIDATION_ERROR", "details": {
      "field_path": "/input/query", "reason": "required field missing" }, ... } }
  ```

**Output size cap (HARD — 10 MiB inline maximum):**
- Tool response body MUST NOT exceed 10 MiB.
- Tools whose natural output exceeds this MUST use the S3-reference pattern shown above:
  - Write the body to `s3://cypherx-tools-output-<env>/<tenant_id>/<invocation_id>` with SSE-KMS.
  - Return `output.ref` instead of the inline body.
  - xAgent fetches via pre-signed URL with the same 10 MiB ceiling per fetch (chunked retrieval if larger).
- Bucket lifecycle: objects deleted after 24h (tool output is transient).

**Idempotency (per Contract 9):**
- Tools whose manifest declares `idempotent: true` MUST honour `Idempotency-Key: <uuid>`:
  - Storage: Redis (per-tool deployment), TTL 24h.
  - Key shape: `(tool_name, version, idempotency_key)`.
  - Cached responses include the original `trace_id` so audit replay is intact.
- Tools with `idempotent: false` MUST reject `Idempotency-Key` with `VALIDATION_ERROR` rather than accept-and-misbehave.

**Tool versioning** (catalogue — Tool Registry, Component 1):
- Each tool publishes its manifest with a semver `version` field.
- Agents pin a version in `xagent.agents.allowed_tools` as `"tool-web-search@1.2.0"` (or `"@latest"` for opt-in latest).
- Each version is a separate K8s Deployment + Service. The Registry stores the version-specific endpoint URL (Component 1).
- Deprecation: a `status='deprecated'` version is still callable but every response carries `Deprecation: true` and `Sunset: <RFC 7231 date>` headers (Contract 9).
- Max 3 concurrent versions per tool (see Component 1 retention policy).

---

### Component 3 — tool-web-search ⚡

**What it does:** Search the web and return ranked results with snippets.

**Tools exposed in manifest:**
```
web_search:
  input:  { query: string, max_results: integer (1-20) }
  output: { results: [{ title, url, snippet, rank }] }
  timeout: 30s
  idempotent: true
```

**Implementation approach:**
- Provider: SerpAPI or Brave Search API. Selected at startup via env var `SEARCH_PROVIDER ∈ {serpapi, brave}`. Provider-specific keys: `SERPAPI_API_KEY`, `BRAVE_API_KEY`.
- Rate limit: 10 req/sec per tenant via Valkey sliding window. **Fail-open with telemetry on Valkey outage** (matches LLMs gateway Phase 3 Component 5):
  - On Valkey error, skip rate-limit enforcement.
  - Emit Prometheus counter `tool_ratelimit_skipped_total{tool="tool-web-search", reason="valkey_unavailable"}`.
  - Log WARN per request.
  - Alertmanager fires at ≥ 10/min for ≥ 2 min.
  - `/readyz` does NOT fail — rate limiter is a cost guard, not a security gate.
- Sandboxing: no sandbox needed (no code execution — only HTTP calls to search API).

> 🏗️ **Internal architecture for tool-web-search must be planned separately before implementation.**

---

### Component 4 — tool-code-exec 📋

**What it does:** Execute sandboxed code (Python, JavaScript, shell).

**Critical security requirement:**
- Runs in gVisor (sandboxed container runtime) — escape from container is blocked at syscall level
- No network access from inside the sandbox
- Filesystem: ephemeral per-execution tmpfs, 100MB limit
- CPU time limit: 30s hard cap
- Memory limit: 256MB

> 🏗️ **Internal architecture for tool-code-exec must be planned separately. Sandboxing implementation is safety-critical.**

---

### Component 5 — tool-http-client 📋

**What it does:** Make arbitrary outbound HTTP requests on behalf of an agent.

**Security controls:**
- SSRF protection: blocklist of private IP ranges (10.x.x.x, 172.16.x.x, 192.168.x.x, 169.254.x.x)
- Allowlist mode: per-tenant configurable domain allowlist
- Redirects: follow max 3 redirects only
- Timeout: 30s
- Max response size: 10MB

---

### Component 6 — tool-file-ops 📋

**What it does:** Read/write/list files in a sandboxed per-agent workspace.

**Storage:** S3-backed workspace per agent (path: `s3://cypherx-tools-storage/{tenant_id}/{agent_id}/workspace/`)
**Operations:** read, write, list, delete files within workspace
**Constraints:** max workspace size 1GB per agent, max file size 100MB

---

### Component 7 — tool-email 📋
### Component 8 — tool-calendar 📋
### Component 9 — tool-browser 📋
### Component 10 — tool-image-gen 📋
### Component 11 — tool-pdf-gen 📋
### Component 12 — tool-data-analysis 📋
### Component 13 — tool-notify 📋

> 🏗️ Each tool above requires its own separate architecture plan before implementation. Refer to Section 4.7 of the master platform plan for capability descriptions.

---

### Kafka Events — Tools Domain ⚡ (AMENDED 2026-06 — see Amendment Log)

**`cypherx.tools.invocation.metered`** — emitted on EVERY tool invocation (NOT only
high-stakes tools), from **xAgent's transactional outbox (`xagent.outbox`)**, NOT from
the tool servers. Tool servers are stateless (no database) and therefore cannot host an
outbox; xAgent already records every tool call in `xagent.task_steps` in the same
transaction and is the only first-cycle caller, so it is the single emitter
(Contract 14 single-owner rule). The topic also appears in phase-09's `xagent.outbox`
topic list (the emitting side).

Payload:

```json
{
  "tool":         "tool-web-search",
  "version":      "1.0.0",
  "agent_id":     "<uuid>",
  "tenant_id":    "<uuid>",
  "duration_ms":  234,
  "output_bytes": 18244,
  "status":       "succeeded",
  "request_id":   "<uuid>"
}
```

- Partition key: `tenant_id`.
- The payload carries **units + `request_id` correlation ONLY** — no unit costs and no
  cross-schema joins to `llms` pricing tables. Billing joins/rollups happen downstream
  in the usage pipeline, which de-duplicates on `request_id`.
- `publisher_tenant_id` / `consumer_tenant_id` revenue-share attribution fields are 📋
  (marketplace wave) — added when third-party publishers can be billed.
- `cypherx.tools.invocation.completed` (high-stakes audit event for code-exec /
  http-client / file-ops) remains 📋 — see the enterprise checklist.
- Topic provisioning: idempotent `topics-init` compose job (`rpk topic create`) first
  cycle; Terragrunt in the cloud form (see Compose-Parity subsection).

---

### Compose-Parity Runtime (first cycle — AMENDED, see Amendment Log)

The first-cycle runtime is **docker compose + Neon (Postgres) + Valkey + Redpanda + MinIO**.
There is NO K8s, Kong, Istio, Doppler, AWS, Argo, or gVisor in the first cycle. The K8s/Istio
specs below are the **deploy-target (cloud) form**, conditional on the infra phase. Compose
equivalents:

- **Services:** `tool-registry` and `tool-web-search` are two compose services; same images,
  env-driven config, `/livez`/`/readyz` wired as compose `healthcheck`s. The version-specific
  "K8s Service DNS" becomes a compose service name (e.g. `http://tool-web-search:8080`);
  `registry.tools.endpoint_url` values — including the platform-tool seed migration — are
  env-substituted per environment, never hardcoded to cluster DNS.
- **Config/secrets:** the "from Doppler" env-var set maps 1:1 to compose `.env` / environment
  blocks (Doppler injection is the cloud form). `AUTH_SERVICE_URL`/`AUTH_JWKS_URL` point at
  compose service DNS (e.g. `http://auth:8080/...`).
- **Large-output storage:** `S3_TOOLS_OUTPUT_BUCKET` + `S3_ENDPOINT` + `S3_SSE_MODE ∈ {none, kms}`
  fully env-driven (fix #21 convention). First cycle: MinIO with `S3_SSE_MODE=none`; SSE-KMS
  (`S3_TOOLS_OUTPUT_KMS_KEY_ID`) is the cloud form. The 24 h transient-output lifecycle is a
  MinIO ILM rule locally, an S3 lifecycle policy in the cloud.
- **Caller restriction (Istio AuthorizationPolicy stand-in):** enforcement is in-app — tools
  already verify the Contract 12 service JWT; first cycle they additionally check the service
  token's `sub` against an env-driven caller allow-list (`/mcp/v1/invoke`: xagent only;
  `/manifest`: tool-registry). The Istio policy below is the cloud form of the same rule.
- **Sandboxing (`runtimeClassName: gvisor`):** deploy-target. First-cycle tools (registry +
  tool-web-search) execute no untrusted code; `sandbox_class` is validated and RECORDED at
  registration time so marketplace tools deploy correctly once the cloud form lands.
- **Kafka topics:** idempotent `topics-init` compose job (`rpk topic create`, safe to re-run)
  stands in for Terragrunt-provisioned topics. Topic names/partitions identical.
- **HPA / node selectors / `tools-public` namespace + NetworkPolicy + Kong:** deploy-target
  mechanisms; the first-cycle compose stand-in for marketplace-tool isolation is a separate
  compose network with no route to core services except the published gateway port.

---

### Tool K8s Standard Deployment Pattern (deploy-target / cloud form — conditional on the infra phase)

```yaml
# Every MCP tool follows this pattern (per-version Deployment + Service)
Namespace:   tools
Deployment:  tool-<name>-v<major>-<minor>-<patch>     # e.g. tool-web-search-v1-2-0
Service:     tool-<name>-v<major>-<minor>-<patch>     # cluster-DNS resolved by Registry
Replicas:    min 2, max 8 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: tools

Resources (default — adjust per tool):
  requests: { cpu: 200m, memory: 256Mi }
  limits:   { cpu: 1000m, memory: 512Mi }

# tool-code-exec special settings:
runtimeClassName: gvisor        ← sandboxed runtime
securityContext:
  runAsNonRoot: true
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false

Health probes (Contract 7 — every tool):
  livenessProbe:  GET /livez   (process-only; period 10s)
  readinessProbe: GET /readyz  (provider/backing API reachable; period 5s)

Env vars (env-driven — compose `.env` first cycle; Doppler-injected in the cloud form — standard set for every tool):
  AUTH_SERVICE_URL              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET      (Contract 12; from service-auth/<tool-name>/bootstrap_secret)
  KAFKA_BROKERS
  VALKEY_URL                    (rate limiting + idempotency cache)
  S3_TOOLS_OUTPUT_BUCKET        (cypherx-tools-output-<env>; required for large-output tools)
  S3_TOOLS_OUTPUT_KMS_KEY_ID    (alias/cypherx-tools-output-<env>)

# Tool-specific (additive):
  tool-web-search:
    SEARCH_PROVIDER             (serpapi | brave)
    SERPAPI_API_KEY             (when SEARCH_PROVIDER=serpapi)
    BRAVE_API_KEY               (when SEARCH_PROVIDER=brave)
```

> **Service ACL (cross-phase Phase 2 update — required when Phase 7 deploys):**
> Phase 7 migration extends `auth.service_acl` with idempotent INSERTs:
> - `xagent → tool-registry [internal:read]`
> - `xagent → tool-web-search [internal:write]`
> - `tool-registry → auth-service [internal:read]`
> - `tool-registry → tool-web-search [internal:read]` (manifest poll + health)
> - `tool-web-search → auth-service [internal:read]`
>
> Each future tool added per phase ships its own pair of ACL rows (`xagent → tool-X`,
> `tool-X → auth-service`, and `tool-registry → tool-X`). No wildcards.
>
> **JWKS verification** follows the Phase 3 pattern: in-cluster URL only, 5-min cache,
> refresh-on-`kid`-miss rate-limited to 1/min. Applies to every tool.

---

### Istio Authorization Policy for Tools (deploy-target / cloud form — compose stand-in: in-app caller allow-list, see Compose-Parity)

```yaml
# Tools accept /mcp/v1/invoke only from xagent.
# Tools accept /manifest + /livez + /readyz + /metrics from tools (Registry) and observability.
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: tools-allow-callers
  namespace: tools
spec:
  action: ALLOW
  rules:
    # Invoke path: xAgent only
    - from:
        - source: { namespaces: [xagent] }
      to:
        - operation: { paths: ["/mcp/v1/invoke"] }
    # Manifest + health: Tool Registry (same namespace) + observability scrape
    - from:
        - source: { namespaces: [tools, observability] }
      to:
        - operation: { paths: ["/manifest", "/livez", "/readyz", "/metrics"] }
```

---

## ⚡ First Cycle Implementation Checklist

> **Checklist split (AMENDED — see Amendment Log):** "Service code" items are buildable in the
> actual first-cycle runtime (compose + Neon + Valkey + Redpanda + MinIO). "Deploy-target" items
> are the cloud (K8s/Istio/Argo/Kong) form, conditional on the infra phase, with compose
> equivalents documented in the Compose-Parity subsection.

### Service code (compose-buildable)

- [ ] Tool Registry service architecture planned separately
- [ ] **`registry.tools` schema** (not `platform.tools`) — Tool Registry owns the schema per Contract 14
- [ ] **`auth_required` and `manifest_url` columns DROPPED** (dead/redundant)
- [ ] Tool Registry endpoints: list / get-by-name / get-by-name@version / register / health-check
- [ ] **Version-specific `endpoint_url`** stored and returned by discovery — matches the versioned deploy unit (compose service DNS first cycle; versioned K8s Service in the cloud form); `endpoint_url` values env-substituted, never hardcoded (amended)
- [ ] **`uq_tools_name_latest` partial unique index** (one `is_latest=true` per tool name)
- [ ] **Platform-tool seed migration** (`registry/db/migrations/*__seed_platform_tools.sql`) — idempotent ON CONFLICT, ships at least `tool-web-search` row
- [ ] **Registry self-bootstrap** — eager manifest fetch for all `is_latest=true` rows on startup; `/readyz` gated on initial poll completing
- [ ] Health-check background job (30s `/livez` poll, Valkey caching, 3→degraded / 5→offline transitions)
- [ ] **Manifest ETag-aware polling** (re-fetch only on If-None-Match 200)
- [ ] **Version retention policy** — max 3 concurrent versions per tool; registry refuses a 4th without override
- [ ] `tool-web-search` architecture planned separately
- [ ] `tool-web-search` MCP server: `web_search` tool, JWKS-based JWT validation, rate limiting
- [ ] **Tool-web-search fail-open rate-limiter** with telemetry on Valkey outage
- [ ] **Single auth pattern** on `POST /mcp/v1/invoke` (service JWT + `X-Forwarded-Agent-JWT`); identity rejected in body
- [ ] **Both scope checks** enforced (`tool:invoke` AND `tool:<server-name>:invoke`) per Contract 4 post-edit
- [ ] **Server-side input validation** against manifest `input_schema`; 422 `VALIDATION_ERROR` with JSON Pointer
- [ ] **10 MiB output cap**; S3-reference pattern (`output.ref`) for larger payloads — bucket/endpoint/SSE mode fully env-driven (`S3_TOOLS_OUTPUT_BUCKET`, `S3_ENDPOINT`, `S3_SSE_MODE ∈ {none, kms}`; MinIO + `none` first cycle, SSE-KMS is the cloud form — amended); 24 h transient-output lifecycle
- [ ] **Idempotency-Key honored** on `idempotent: true` tools; rejected on `idempotent: false` tools (per Contract 9)
- [ ] **`/livez`, `/readyz`, `/metrics`, `/manifest`, `/mcp/v1/invoke`** — every tool exposes exactly these (manifest path UNVERSIONED)
- [ ] **Prometheus histogram** for `tool_invocation_duration_seconds_*` (NOT `quantile` label)
- [ ] Response field `duration_ms` (not `latency_ms`)
- [ ] **Service ACL migration** seeds: `xagent → tool-registry`, `xagent → tool-web-search`, `tool-registry → auth-service`, `tool-registry → tool-web-search`, `tool-web-search → auth-service`
- [ ] **In-app caller allow-list** (compose-parity stand-in for the Istio `AuthorizationPolicy` — amended): service-token `sub` checked against an env-driven allow-list — `/mcp/v1/invoke` accepts xagent only; `/manifest` accepts tool-registry (cloud form in the deploy-target section)
- [ ] Both services run in the first-cycle compose stack (registry + tool-web-search) with `/livez`/`/readyz` healthchecks — compose-parity for the K8s `tools`-namespace + ArgoCD deploy (cloud form in the deploy-target section; amended)
- [ ] **Idempotent `topics-init` compose job** (`rpk topic create`, safe to re-run) provisions `cypherx.tools.*` topics — Terragrunt stand-in per the Compose-Parity subsection (amended)
- [ ] **`registry.tenant_tools` table + SPLIT RLS policies** (amended — see Amendment Log): `FOR SELECT` keeps the marketplace-visibility OR-clause; `FOR INSERT`/`UPDATE`/`DELETE` are tenant-only with USING + WITH CHECK — private/tenant-public/marketplace tools; mixed-scope discovery returns UNION
- [ ] **External publisher submission flow** — `POST /v1/tenant-tools` with Trivy/Snyk image scan, sandbox_class validation + recording (gvisor default for non-platform tools; the gVisor runtime itself is deploy-target — amended), egress allowlist lint, `pending_review → active` lifecycle
- [ ] **Per-tenant tool quota enforcement** — `private_tools_max`, `publishable_versions_max`, `invocations_per_min` from `auth.tenant_quotas` (Contract 19)
- [ ] **Per-invocation metering event** — `cypherx.tools.invocation.metered` on EVERY tool invocation (NOT only high-stakes), emitted from **xAgent's `xagent.outbox`** (tool servers are stateless — there is no tool-side outbox; amended, see Amendment Log); payload: `tool`, `version`, `agent_id`, `tenant_id`, `duration_ms`, `output_bytes`, `status`, `request_id` — units + correlation only, billing joins downstream (`publisher_tenant_id`/`consumer_tenant_id` revenue-share fields → 📋 marketplace wave)
- [ ] **Capability layer (Component 1c) ⚡** — `registry.capabilities`, `tool_capabilities` (with its OWN RLS policy, USING + WITH CHECK — amended, see Amendment Log), `tenant_capability_bindings`; seed 6 platform capabilities; resolver gives tenant-bound tool first, platform default fallback

### Deploy-target (cloud form — conditional on the infra phase; compose equivalents in the Compose-Parity subsection)

- [ ] **Istio `AuthorizationPolicy`** — `/mcp/v1/invoke` from xagent only; `/manifest`, `/livez`, `/readyz`, `/metrics` from tools (Registry) + observability (compose stand-in: in-app caller allow-list, above)
- [ ] Both deployed to K8s (tools namespace) via ArgoCD (compose first cycle: services in the stack — see above)
- [ ] **`tools-public` namespace + NetworkPolicy** — tenant marketplace tools deploy here under `runtimeClassName: gvisor`; cannot reach `shared-core` services except via `Authorization: Bearer <jwt>` through Kong (compose stand-in: isolated compose network with no route to core services)

## 📋 Full Enterprise Implementation Checklist

- [ ] `tool-code-exec` architecture planned + implemented (gVisor sandbox)
- [ ] `tool-http-client` with SSRF protection
- [ ] `tool-file-ops` with S3-backed workspace
- [ ] `tool-email`, `tool-calendar`, `tool-browser`, `tool-image-gen`
- [ ] `tool-pdf-gen`, `tool-data-analysis`, `tool-notify`
- [ ] Anthropic-MCP bridge (`tool-mcp-bridge`) for external Claude Desktop / IDE clients
- [ ] Per-tenant tool allowlist (`registry.tenant_tool_acl`) — first cycle is all-tenants-see-all
- [ ] `cypherx.tools.invocation.completed` Kafka event for high-stakes tools (code-exec, http-client, file-ops) via outbox
- [ ] Version teardown automation (oldest deprecated version → K8s teardown after 30d)
- [ ] Tool marketplace metadata (public registry post-SDK)
- [ ] Per-tool metrics dashboards in Grafana

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Registry Becoming a Runtime Bottleneck — REAL
Evidence: lines 61–69. Registry queried per invocation; no client-side manifest cache.
**Mitigation:** xAgent caches manifest (tool list, schemas) locally for 5 min with ETags; registry returns 304 on no-change. Valkey holds per-tenant tool list.

### 2. Capability Layer Missing Version Compatibility Rules — PARTIAL
Evidence: lines 284–341. Capabilities exist; no compat matrix.
**Mitigation:** tools tagged with a capability declare `min_version` / `max_version` in `tool_capabilities`; resolver rejects mismatches at bind time.

### 3. Marketplace Tools as the Highest-Risk Security Surface — REAL
Evidence: lines 196–239. Scanning + sandbox documented; container signing absent.
**Mitigation:** marketplace tools must ship cosign-signed OCI images; registry verifies signature against publisher key before activation; post-execution attestation in immutable audit (Contract 11).

### 4. Missing Execution Lineage Model — REAL
Evidence: lines 49–94. `trace_id` propagated; no parent-tool-call linkage.
**Mitigation:** add `X-Parent-Tool-Call-Id: <uuid>` to MCP headers and audit a lineage stack per call.

### 5. Large Output S3 Pattern Missing Orphan Lifecycle Handling — REAL
Evidence: lines 374–382, 427–433. 24 h TTL; no orphan reconciliation.
**Mitigation:** nightly scan matches `s3://.../{invocation_id}` against completed `tool_calls` rows; orphans deleted ahead of TTL.

### 6. Missing Explicit Streaming Strategy — REAL
Evidence: lines 345–407. All endpoints JSON; no SSE / chunked path.
**Mitigation:** manifest declares `streaming: true`; client sends `Accept: text/event-stream`; server emits `{"chunk": ..., "seq": N}` then `{"done": true, "output": {...}}`.

### 7. Tool Registry Polling Model Scalability Limits — REAL
Evidence: lines 259–273. 30 s poll.
**Mitigation:** at >100 tools, switch to webhook push (tools POST `/health-notify` to registry on state change) with 5-min poll fallback; Valkey cluster stores health; DNS returns only healthy endpoints.

### 8–13. Capability Abstraction / Platform vs Tenant Separation / Auth Propagation / Versioned Deployments / Liveness vs Readiness / S3 Offload — ALL VERIFIED
Evidence: lines 284–341, 116–119, 70–90, 442–447, 268–271, 374–382. Well-designed; no fix needed.
