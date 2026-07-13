# Flow‑Tool‑Builder — Visual No‑Code MCP Tool Creation (Node‑RED execution backend)

> Engine decision: **Node‑RED (Apache‑2.0)**, not n8n — see §1 Context. The folder name `n8n-implementation` is kept for continuity; consider renaming to `flow-tool-builder`.

---

## 1. Context — why this is being built

CypherX is an MCP‑centric, multi‑tenant, commercial Agentic‑AI platform. Agents discover and call tools **only** through MCP; they never know how a tool is implemented. We want customers to build tools **visually, no‑code**, click **Publish**, and have the workflow become an MCP tool that any connected agent can immediately discover and call — **without ever hand‑writing MCP JSON**.

**Why not n8n (verified):** n8n is "fair‑code" (Sustainable Use License). Its LICENSE.md restricts free use to *internal business purposes*; embedding the editor in a paid multi‑tenant product is explicitly disallowed ("white‑labeling n8n and offering it to your customers for money") and requires a paid **Embed/OEM license** (~$50k/yr, unpublished; branding still shows). Link‑out/iframe/forking do **not** cure it.

**Engine chosen — Node‑RED (Apache‑2.0):** A 21‑agent license‑verification pass (each license read from the actual GitHub `LICENSE` file) found only three engines are cleanly permissive for commercial embedding: **Node‑RED (Apache‑2.0)**, **Elsa (MIT)**, **Langflow (MIT)**. Node‑RED wins for CypherX: same **Node.js** runtime as the frontend/BFF (Elsa drags in .NET/Blazor), most mature, best mount‑as‑library white‑label story, and `HTTP In → HTTP Response` is a 1:1 match for a synchronous MCP tool call. Its engineer‑flavored UX is a non‑issue for a **developer‑facing** platform.

**Invariant (the whole point of the design):** Node‑RED is **only an execution backend + editor**. It is **NOT** the Tool Registry, **NOT** the MCP server, **NOT** the source of truth. All tool identity, schema, auth, tenancy, discovery, and the MCP wire protocol live in CypherX. Because execution is abstracted behind a small **HTTP‑trigger adapter**, the engine is swappable (Node‑RED → Elsa → Langflow) by reimplementing one adapter class — nothing else changes.

**Outcome:** Tool Builder page → build flow in embedded, white‑labeled Node‑RED → **Publish Tool** (friendly form, no JSON) → a Contract‑4 MCP tool is registered in the existing Tool Registry → agents discover it via the registry → agent calls it → xAgent invokes the new bridge → execution routed to the tenant's Node‑RED over HTTP → result returned as an MCP tool result.

### 1.1 Engine comparison (verified against primary-source `LICENSE` files)

| Engine | License (verified) | Stack | Sync HTTP trigger | Commercial embed | Verdict |
|---|---|---|---|---|---|
| **Node‑RED** | **Apache‑2.0** (OpenJS) | Node.js | `HTTP In → HTTP Response` | ✅ free | **CHOSEN** |
| Elsa Workflows | MIT (engine + designer) | .NET/Blazor | `HttpEndpoint → WriteHttpResponse` | ✅ free | Alt (stack mismatch) |
| Langflow | MIT | Python | `/run?stream=false` + `/webhook` | ✅ free | Alt (AI‑flow‑centric) |
| n8n | fair‑code (Sustainable Use License) | Node.js | Webhook → Respond to Webhook | ❌ paid Embed (~$50k/yr) | Rejected (license) |
| Activepieces | MIT core + paid EE | Node.js | Webhook `/sync` | ❌ embed SDK is paid EE | Rejected |
| Windmill | AGPL‑3.0 + proprietary SDK | Rust/TS | `/jobs/run_wait_result` | ❌ AGPL + commercial | Rejected |
| Flowise / Kestra | Apache core, commercial tenancy | Node / Java | prediction / webhook | ⚠️ conditional | Rejected |
| Camunda / Directus / Budibase / Conductor / Automatisch | source‑available / BSL / GPL / AGPL / builder is enterprise | — | varies | ❌/⚠️ | Rejected |

**Compliant ways to still use n8n (if ever desired):** (1) buy the Embed/OEM license (only license‑clean path for customer‑facing embed; branding still shows); (2) internal‑only use where *your* team owns all workflows and customers never see the canvas (free, but not our use case). Link‑out, iframe, and forking do not change the outcome.

---

## 2. Grounding facts (from codebase exploration — build against these)

**Tool model / registry** (`Tools/tool-registry/`, Python/FastAPI, source of truth):
- A "tool" is an MCP **server** (dash‑case `name`) that serves Contract‑4 `GET /manifest` and `POST /mcp/v1/invoke`, plus `/livez` `/readyz` `/metrics`.
- Register: `POST /v1/tools` where **the request body IS the raw Contract‑4 manifest** (`api/tools.py:122`). `base_url` lives *inside* the manifest and drives BOTH the invoke URL and the health‑poll target `{base_url}/manifest` (`services/discovery.py:resolve_invoke_url`). Re‑publish a new version: `POST /v1/tools/{name}/versions` (name must match; retention keeps newest 3 active).
- `tenant_id` + scopes come **only from the JWT** (Contract 13). INTERNAL auth mode = service token (`sub` `svc:*`) + `X-Forwarded-Agent-JWT`; the effective principal (tenant + `tool:admin` scope) is taken from the **forwarded agent JWT** (`core/auth.py:189`). So the publishing user's JWT must carry `tool:admin`.
- Access governance: `POST /v1/restricted-tools/{name}` (mark restricted) + `PUT /v1/tools/{name}/access` (`{agent_id, access_mode: none|ask|automated, capability?}`), scope `tenant:admin`. Migrations: `Tools/tool-registry/db/migrations/*` (schema `tools`; split‑RLS `WITH CHECK` marketplace‑hole fix).

**Invoke wire protocol** (from `xAgent/ax-1/src/agent_runtime/services/mcp_client.py`, reference server `Tools/tool-web-search/api/invoke.py`):
- xAgent resolves `GET {registry}/v1/tools/{server}?version=` → `invoke_url`, then `POST {invoke_url}/mcp/v1/invoke` with body `{"tool": "<snake>", "args": {...}}` → expects `{"tool": "...", "result": {...}}`.
- Headers: `Authorization: Bearer <svc jwt>`, `X-Forwarded-Agent-JWT`, `traceparent`, `X-Request-ID`, `Idempotency-Key: <task_id>:<tool_call_id>`.
- **4xx = terminal (never retried)**; **5xx/transport = retried with the same Idempotency-Key** + circuit‑breaker after 5 fails. Errors must be Contract‑2 envelopes. 10 MiB output cap.
- Access enforced by xAgent at call time via `GET {registry}/v1/tools/{server}/access?capability=<tool>` → `none|ask|automated`, **fail‑closed**. `ask` → HIL approval.
- Tool servers emit **no** metering — xAgent's outbox owns it. Do not meter in the bridge.

**Scaffold to fork:** there is **no shared MCP SDK**. `Tools/tool-web-search/` is the canonical stateless MCP‑server scaffold (`main.py` create_app; `api/{health,manifest,invoke}.py`; `core/{auth,config,errors,trace,metrics,valkey,body_limit}.py`; `services/manifest.py` build+validate; `services/{idempotency,rate_limit}.py`; `services/providers/*` ← the swappable backend). `core/auth.require_principal` is the reusable dual‑mode JWT+revocation verifier. Dockerfile: `python:3.12-slim`, `uv sync --frozen`, non‑root uid 10001, `PORT=8080`. `mcp-eng-memory` (`CoreProjects/cypherx-a1/mcp-eng-memory/`) is a second copy that proves the pattern of fronting a backend with many tools.

**Service→service auth (Contract 12):** mint a short‑lived service JWT from Auth via `POST {auth}/v1/service-tokens` with `X-Service-Name` + `X-Service-Bootstrap-Secret`, body `{on_behalf_of: <agent_id>}`; cache per `on_behalf_of`. Pattern verbatim in `CoreProjects/cypherx-a1/src/cypherx_a1/services/service_token.py`. Seed an `auth.service_acl` edge granting the bridge `tool:admin` (mirrors `cypherx-a1`'s `_0002__seed.sql`).

**Frontend / BFF** (`frontend/`):
- Pages: `src/app/(app)/<feature>/page.tsx`, `'use client'`, `<Page><PageHeader/><PageBody fill>…</PageBody></Page>` (use `fill` for the editor). A `/tools` **registry catalog** page already exists — the Builder is a new sub‑route `/tools/builder`.
- Data path: page → `lib/services.ts` wrapper → `lib/bff-client.ts` `api(service, path)` → `/bff/api/<service>/*`. `ProxyService` union in `bff-client.ts:284` is the allow‑list. Nav: static `NAV` array in `components/AppShell.tsx` ("Capabilities" group has Tools/Skills); `NavItem` supports `scope?`.
- BFF (`frontend/bff/`, Fastify): `/bff/api/*` proxy resolves upstream by first path segment (`proxy/index.ts`), strips client identity headers and injects `Authorization: Bearer <session downstream JWT>` + `X-Tenant-ID` (`proxy/headers.ts`). Upstreams from env (`config/index.ts parseUpstreams`, e.g. `['tools','TOOL_REGISTRY_URL']`). SPA has **no CSP** today; proxying the editor through the BFF makes the iframe same‑origin (no frame‑ancestors problem).

**Deployment** (`infra/compose/docker-compose.yml`, `charts/cypherx-service`, `gitops/`): every app service listens on `PORT=8080` in‑container, addressed by compose DNS, `depends_on: valkey + auth-service`, stdlib `/livez` healthcheck. Next free host port = **8096**. Services consume the shared `charts/cypherx-service` chart via a values‑only wrapper (see `charts/example-service/values.yaml`; `nodeRole: tools`, `database.enabled`). GitOps app‑of‑apps under `gitops/envs/<env>/tools/<svc>/` (prod has no `syncPolicy.automated`).

**Node‑RED facts** (verified from nodered.org docs):
- Embed: `RED.init(httpServer, settings)`, mount `RED.httpAdmin` at `httpAdminRoot` + `RED.httpNode` at `httpNodeRoot`, `RED.start()`. `userDir` = per‑instance storage. `editorTheme` white‑labels (page title, favicon, CSS, header, deploy button, menu). `httpAdminRoot:false` disables the editor entirely.
- Deploy flows: Admin API `GET /flows`, `POST /flows` (perm `flows.write`), `GET/POST/PUT/DELETE /flow[/:id]`; bearer‑token auth via `adminAuth` (static `adminAuth.tokens` or a custom `tokens(token)` validator function).
- Synchronous tool trigger: `HTTP In` (method+path under `httpNodeRoot`) → … → `HTTP Response` (returns `msg.payload` + status). Optional `httpNodeAuth` (basic) or a header‑check.
- Hardening: `externalModules:{palette:{allowInstall:false}}` / `editorTheme.palette.editable:false` to block ad‑hoc node installs; `credentialSecret` for encrypted credentials; `functionExternalModules:false`; run non‑root; restrict egress at the network layer.

---

## 3. Target architecture

```
                            ┌───────────────────────── CypherX (owns identity, schema, MCP) ─────────────────────────┐
 Browser (SPA)              │                                                                                        │
  /tools/builder  ──iframe──┼─▶ BFF /bff/nodered/*  ──(inject tenant admin token, WS)──▶  Node‑RED (tenant T)        │
   Publish dialog ──────────┼─▶ BFF /bff/api/toolbuilder/*  ──▶  tool‑flow‑bridge  ─────┐   [editor + HTTP‑In nodes] │
                            │                                     │ (control plane)     │            ▲               │
                            │                                     ├─ Node‑RED Admin API─┘            │ HTTP‑In       │
                            │                                     ├─ register manifest ─▶ Tool Registry (unchanged)  │
                            │                                     └─ persist binding (flow_tools DB, RLS)            │
   Agent (via xAgent) ──────┼─▶ GET /v1/tools/tool-<slug> ─▶ invoke_url ─▶ tool‑flow‑bridge /w/<slug>/mcp/v1/invoke ─┼─▶ Node‑RED HTTP‑In (tenant T) ─▶ HTTP Response ─▶ result
                            └────────────────────────────────────────────────────────────────────────────────────────┘
```

**Components:**

| # | Component | New/changed | Role |
|---|-----------|-------------|------|
| A | **`tool-flow-bridge`** service (Python/FastAPI) | **NEW** | Control plane (publish, provisioning, editor sessions) + data plane (per‑workflow MCP `/w/<slug>/{manifest,mcp/v1/invoke}`). Owns DB schema `flow_tools`. |
| B | **Node‑RED tenant runtime** | **NEW** | Hardened Node‑RED image + one instance per tenant (editor + HTTP‑In endpoints). The *execution backend*. |
| C | **Tenant runtime provisioner** | **NEW** (inside A) | Creates/tracks per‑tenant Node‑RED (k8s adapter for prod, docker/compose adapter for dev). |
| D | **Frontend Tool Builder page** `/tools/builder` | **NEW** | Embeds the tenant's Node‑RED editor (iframe via BFF) + Publish dialog + published‑tool list. |
| E | **BFF wiring** | **CHANGED** | New `toolbuilder` upstream; new `/bff/nodered/*` editor proxy (tenant‑scoped, admin‑token injection, WebSocket). |
| F | **Tool Registry** | **UNCHANGED** | The bridge registers `tool-<slug>` servers via its existing API. |
| G | **xAgent** | **UNCHANGED** | Discovers + invokes via the standard Contract‑4 path. |
| H | **Auth** | **seed only** | `auth.service_acl` edge granting `tool-flow-bridge` → `tool:admin`; bootstrap secret. |

**Modeling decision — one registry server per published workflow (`tool-<slug>`), not one shared server.** Each published workflow registers as its own MCP server with `base_url = http://tool-flow-bridge:8080/w/<slug>` and one snake_case tool. This gives per‑tool scope (`tool:tool-<slug>:invoke`), per‑tool health, per‑tool versioning, per‑tool access mode, and tenant‑shadows‑platform — and it sidesteps the registry's 3‑active‑versions retention cap that a single shared `tools[]` server would hit. The bridge multiplexes all workflows by the `/w/<slug>` path prefix.

**Tenant isolation model (full production).** Node‑RED core is single‑workspace, so isolation = **one Node‑RED instance per tenant** (container/pod), each with its own `userDir` (PVC), `credentialSecret`, hardened `settings.js`, resource limits, non‑root, and an **egress‑deny NetworkPolicy** (workflows make arbitrary HTTP — they must not reach internal platform services or other tenants). Instances are provisioned on first Tool‑Builder open and may scale‑to‑zero when idle. Strong‑isolation option: namespace‑per‑tenant + `gVisor`/`Kata` runtimeClass (mirrors the platform's planned code‑exec/gVisor tool).

---

## 4. Code‑level contracts (the exact shapes)

### 4.1 Contract‑4 manifest the bridge generates per published workflow
```json
{
  "schema_version": "1.0.0",
  "protocol_version": "mcp/1.0",
  "name": "tool-<slug>",                      // dash-case, unique per tenant
  "display_name": "<user title>",
  "version": "1.0.0",                          // bumped on re-publish
  "description": "<user description>",
  "author": "tenant:<tenant_id>",
  "category": "flow-tool",
  "base_url": "http://tool-flow-bridge:8080/w/<slug>",   // drives invoke + health poll
  "auth_required": true,
  "required_scopes": ["tool:invoke", "tool:tool-<slug>:invoke"],
  "tools": [{
    "name": "<snake_tool>",                    // single tool per server
    "description": "<user description>",
    "input_schema":  { "type": "object", "properties": { ... }, "required": [ ... ] },
    "output_schema": { "type": "object", "properties": { ... } },
    "timeout_seconds": 30,
    "idempotent": false
  }],
  "health_endpoint": "/livez",
  "metrics_endpoint": "/metrics"
}
```
`input_schema`/`output_schema` are generated by the bridge from the **Publish dialog form** (param name/type/required/description) — the user never writes JSON. If the flow's trigger is a Node‑RED **`http in`** we pre‑fill nothing; if a form/typed trigger is later added, pre‑fill best‑effort. The friendly form → JSON Schema converter lives in the bridge.

### 4.2 Publish API (bridge, called by BFF)
- `POST /v1/flow-tools/publish` → body `{ node_red_flow_id, tool: { title, snake_name, description, input_params[], output_params[], access_mode? } }`.
  1. Fetch the flow via Node‑RED Admin API (`GET /flow/:id`) on the caller's tenant instance.
  2. **Validate shape**: exactly one enabled `http in` (record method+path) and a reachable `http response`; else `422` with a friendly message.
  3. Ensure the flow is deployed/active (Admin API `POST /flow` if needed).
  4. Compute `slug` (`<tenant-short>-<snake>`), derive `input_schema`/`output_schema` from the form.
  5. Upsert a **binding** row (see §5): `slug, tenant_id, node_red_flow_id, http_method, http_path, runtime_id, input_schema, output_schema, version, status`.
  6. Build the manifest (§4.1); register: `POST {registry}/v1/tools` (first) or `POST /v1/tools/{name}/versions` (re‑publish), INTERNAL auth (service token `on_behalf_of` the user's `agent_id` + forwarded user JWT).
  7. Apply access default (**publisher chooses; default `ask`**): if `ask`/`none` → `POST {registry}/v1/restricted-tools/{name}` + `PUT /v1/tools/{name}/access`.
  8. Return `{ slug, server_name, version, invoke_url, access_mode }`.
- `GET /v1/flow-tools` → list this tenant's published tools (for the UI). `DELETE /v1/flow-tools/{slug}` → unpublish (retire in registry + mark binding inactive). `GET /v1/flow-tools/{slug}` → detail.
- `POST /v1/editor-sessions` → returns `{ proxy_base, expires_at }` for the BFF to iframe (provisions the tenant runtime if absent).

### 4.3 MCP runtime API (bridge, called by xAgent) — reuses tool‑web‑search pipeline
- `GET /w/<slug>/manifest` → the stored manifest (ETag/304).
- `POST /w/<slug>/mcp/v1/invoke` → 8‑step pipeline copied from `tool-web-search/api/invoke.py`:
  1. `require_principal` (JWT + revocation), 2. fine scope `tool:tool-<slug>:invoke`, 3. **idempotency replay** (Valkey; essential — flows have side effects), 4. rate limit, 5. `_extract_args` + `input_schema` validation (422 + JSON‑Pointer), 6. **dispatch → Node‑RED HTTP‑In** bounded by `asyncio.wait_for(timeout)`, 7. 10 MiB cap, 8. store + return `{"tool": "<snake>", "result": <json>}`.
  - **Node‑RED adapter (the only engine‑specific code):** `POST http://<runtime_host>:1880{httpNodeRoot}{http_path}` with JSON body = `args`, header `X-CypherX-Tool-Secret: <per-runtime secret>` (matches the flow's inbound auth), plus `traceparent`. Map: Node‑RED 2xx JSON → `result`; Node‑RED 4xx → bridge `422 VALIDATION_ERROR` (terminal); Node‑RED 5xx/timeout/unreachable → bridge `502/503` (retryable). Never leak the runtime host/secret to the agent.
- `GET /livez` `GET /readyz` `GET /metrics`.

---

## 5. Database — new schema `flow_tools` (owned by `tool-flow-bridge`)

Follow `Tools/tool-registry` conventions: role `flow_tools_user` (LOGIN, no BYPASSRLS), tenant‑scoped tables with split RLS (`*_read` own+platform, `*_write` `WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)`), `SET LOCAL app.tenant_id` per tx. Atlas migrations under `Tools/tool-flow-bridge/db/migrations/`.

```sql
-- tenant Node-RED runtimes (one per tenant)
CREATE TABLE flow_tools.tenant_runtimes (
  runtime_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID NOT NULL,
  status         VARCHAR(20) NOT NULL DEFAULT 'provisioning', -- provisioning|running|stopped|error
  internal_host  TEXT NOT NULL,          -- e.g. nodered-<tenant>.tools.svc:1880
  http_node_root VARCHAR(80) NOT NULL DEFAULT '/flow',
  admin_token_ref TEXT NOT NULL,         -- secret ref (Doppler/KMS) for Admin API
  invoke_secret_ref TEXT NOT NULL,       -- secret ref for HTTP-In header auth
  credential_secret_ref TEXT NOT NULL,   -- Node-RED credentialSecret
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id)
);

-- published workflow -> MCP tool bindings
CREATE TABLE flow_tools.tool_bindings (
  binding_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL,
  slug            VARCHAR(100) NOT NULL,     -- -> registry server name tool-<slug>
  snake_name      VARCHAR(100) NOT NULL,     -- MCP tool name
  runtime_id      UUID NOT NULL REFERENCES flow_tools.tenant_runtimes(runtime_id),
  node_red_flow_id TEXT NOT NULL,
  http_method     VARCHAR(10) NOT NULL DEFAULT 'POST',
  http_path       TEXT NOT NULL,             -- the HTTP-In path
  input_schema    JSONB NOT NULL,
  output_schema   JSONB,
  manifest        JSONB NOT NULL,            -- exact Contract-4 manifest last registered
  version         VARCHAR(40) NOT NULL DEFAULT '1.0.0',
  access_mode     VARCHAR(15) NOT NULL DEFAULT 'ask',
  status          VARCHAR(20) NOT NULL DEFAULT 'active', -- active|retired
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, slug)
);
-- + split RLS policies on both tables; indexes on (tenant_id, slug), (tenant_id).
```
Secrets themselves live in Doppler/KMS (only `*_ref` in the DB), matching the platform's secret conventions.

---

## 6. Build phases (execution order)

Each phase ends at a demonstrable checkpoint.

**Phase 0 — Scaffolding.** Fork `Tools/tool-web-search/` → `Tools/tool-flow-bridge/` (rename package `tool_flow_bridge`, keep `core/{auth,config,errors,trace,metrics,valkey,body_limit}`, `api/health`, `services/{idempotency,rate_limit}`). Add `pyproject.toml` deps: `+psycopg[binary,pool]`, `+httpx` (already), `+pydantic`. Create `db/migrations/0001__init.sql` (schema `flow_tools`). Add `.env.example`. **Checkpoint:** `uv run pytest` green (health + auth tests).

**Phase 1 — MCP runtime + Node‑RED adapter (prove execution end‑to‑end first).** Implement `api/manifest.py` (serve stored manifest from DB by slug, ETag) and `api/invoke.py` (the 8‑step pipeline) + `services/nodered_adapter.py` (`invoke(runtime, path, args) -> json`). Seed one binding + a hand‑built Node‑RED flow (`http in`→`function`→`http response`). **Checkpoint:** `curl POST /w/<slug>/mcp/v1/invoke {"tool","args"}` returns `{"tool","result"}` proxied from Node‑RED; register it manually in the registry and drive it through xAgent's tool‑loop.

**Phase 2 — Publish pipeline.** Implement `api/flow_tools.py` (publish/list/delete/detail), `services/nodered_admin.py` (Admin API client: get flow, validate shape, deploy), `services/manifest_builder.py` (form → JSON Schema → Contract‑4), `services/registry_client.py` (register via `POST /v1/tools[/versions]` + access endpoints, Contract‑12 auth via `services/service_token.py` ported from cypherx‑a1). Seed `auth.service_acl` edge (`tool-flow-bridge` → `tool:admin`) + bootstrap secret. **Checkpoint:** `POST /v1/flow-tools/publish` for a real flow → tool appears in `GET {registry}/v1/tools` and is callable via xAgent; re‑publish bumps the version.

**Phase 3 — Provisioning + editor embed + frontend.** Implement `services/provisioner.py` with `KubernetesProvisioner` (prod) + `DockerComposeProvisioner`/`StaticProvisioner` (dev): create per‑tenant Node‑RED (Deployment/StatefulSet + PVC + Service + NetworkPolicy from templates; store `tenant_runtimes` row). Build the hardened Node‑RED image (`Tools/tool-flow-bridge/nodered/`: `Dockerfile`, `settings.js` template, white‑label `editorTheme`). Add BFF: `toolbuilder` upstream (`config/index.ts` + `.env.example` + compose env) and a **`/bff/nodered/*` editor proxy** (resolve session tenant → bridge editor‑session → inject the tenant Node‑RED admin token → proxy incl. WebSocket; align `httpAdminRoot` with the proxy path — known gotcha node‑red#986). Frontend: `frontend/app/src/app/(app)/tools/builder/page.tsx` (`<PageBody fill>` + `<iframe src="/bff/nodered/">`), a **Publish dialog** (Modal + schema‑form), published‑tool list; nav item in `AppShell.tsx`; `lib/services.ts` wrappers via `api('toolbuilder', …)`; `ProxyService` union += `'toolbuilder'`; DTOs in `lib/types.ts`. **Checkpoint:** log in as a tenant, build a flow in the embedded editor, click Publish, see it usable by an agent.

**Phase 4 — Security hardening (full production).** Egress‑deny NetworkPolicy on tenant Node‑RED (default‑deny + explicit allowlist / egress proxy; block platform CIDRs). Palette control (`externalModules.palette.allowInstall:false` + curated allowlist; block/guard `exec`, `file`, `fs`). `functionExternalModules:false`. Per‑runtime `credentialSecret` + `X-CypherX-Tool-Secret` inbound auth on HTTP‑In. Non‑root, read‑only rootfs, seccomp, CPU/mem limits, `runtimeClassName: gvisor` option. Bridge: strict arg validation, output cap, per‑call timeout, idempotency, tenant‑ownership checks on every publish/editor call. Rate‑limit publish. **Checkpoint:** a hostile flow (tries to curl `auth-service`, install a node, spawn a shell) is blocked; automated pen‑style checks pass.

**Phase 5 — Deployment.** Compose: add `tool-flow-bridge` (8096:8080, `depends_on` valkey+auth+tool-registry) + migrations mount/`*_DB_PASSWORD` + a dev Node‑RED image; wire `TOOL_BUILDER_URL` into `frontend-bff`. Helm: `charts/tool-flow-bridge/values.yaml` (+`values-prod.yaml`, `nodeRole: tools`, `database.enabled:true`) and `charts/nodered-tenant/` (per‑tenant template the provisioner renders). GitOps: `gitops/envs/{dev,staging,prod}/tools/tool-flow-bridge/` app + `image.txt` (prod, no `automated:`). **Checkpoint:** `docker compose up` brings the whole path up; `charts` render.

**Phase 6 — Tests + verification.** See §9.

---

## 7. Critical files

**New — `Tools/tool-flow-bridge/`** (forked from `Tools/tool-web-search/`):
- `src/tool_flow_bridge/main.py`, `__main__.py`, `Dockerfile`, `pyproject.toml`, `.env.example`
- `api/health.py` · `api/manifest.py` · `api/invoke.py` · `api/flow_tools.py` (publish/list/delete) · `api/editor_sessions.py`
- `core/{auth,config,errors,trace,metrics,logging,valkey,body_limit}.py` (ported; extend `config` with Node‑RED/registry/auth/provisioner env)
- `services/nodered_adapter.py` (invoke HTTP‑In) · `services/nodered_admin.py` (Admin API) · `services/manifest_builder.py` (form→schema→manifest) · `services/registry_client.py` (register+access) · `services/service_token.py` (Contract‑12, ported) · `services/provisioner.py` (k8s/docker) · `services/idempotency.py` · `services/rate_limit.py`
- `db/pool.py`, `db/queries.py`, `db/migrations/0001__init.sql`
- `nodered/{Dockerfile,settings.js,editorTheme/}` (hardened, white‑labeled image)
- `tests/` (mirror tool‑web‑search test modules + publish/adapter/provisioner)

**Modified:**
- `frontend/app/src/app/(app)/tools/builder/page.tsx` (**new**) · `components/AppShell.tsx` (nav item) · `lib/services.ts` (wrappers) · `lib/bff-client.ts` (`ProxyService += 'toolbuilder'`) · `lib/types.ts` (DTOs) · a `components` Publish dialog
- `frontend/bff/src/config/index.ts` (`['toolbuilder','TOOL_BUILDER_URL',false]`) · `frontend/bff/.env.example` · a new `frontend/bff/src/proxy/nodered.ts` editor proxy (WS + token inject) registered in the BFF app
- `infra/compose/docker-compose.yml` (bridge + dev Node‑RED + migrate mount + BFF env) · `infra/compose/edge/Caddyfile` (only if a direct route is wanted)
- `charts/tool-flow-bridge/` (+ `charts/nodered-tenant/`) · `gitops/envs/*/tools/tool-flow-bridge/`
- Auth seed: `auth.service_acl` edge + bootstrap secret (compose env `SERVICE_BOOTSTRAP_SECRET_FLOWBRIDGE`), and add `flow_tools` to `infra/dev/local/seed/postgres-init.sql` + `infra/modules/postgres-bootstrap/main.tf` (closed enumerations, per cypherx‑a1 precedent)

**Reused (do not reimplement):** `core/auth.require_principal`, the tool‑web‑search invoke pipeline, `service_token.py` pattern, `charts/cypherx-service` base chart, the registry's `POST /v1/tools[/versions]` + access endpoints, the BFF proxy/header trust boundary.

---

## 8. Security model (full production)

1. **Arbitrary‑code isolation:** one Node‑RED per tenant; non‑root, read‑only rootfs, seccomp, CPU/mem limits; optional `gVisor`/`Kata` runtimeClass. No shared workspace.
2. **Egress lockdown:** default‑deny NetworkPolicy; explicit allowlist (or a filtering egress proxy); **block all internal platform CIDRs** so a flow can't reach `auth`, `tool-registry`, other tenants' DBs, or metadata endpoints. This is the primary SSRF/lateral‑movement control.
3. **Palette control:** `externalModules.palette.allowInstall:false` by default; a curated, license‑vetted node allowlist; `exec`/`file`/`fs` nodes disabled or gated; `functionExternalModules:false`.
4. **Auth boundaries:** bridge→registry via Contract‑12 service token; editor via tenant‑scoped admin token injected by the BFF (never in the browser); HTTP‑In tool endpoints require `X-CypherX-Tool-Secret` (bridge‑only caller); per‑runtime `credentialSecret`.
5. **Invoke safety:** JSON‑Schema arg validation (422 + pointer), 10 MiB output cap, per‑call timeout, **idempotency dedup** (side‑effecting flows + xAgent 5xx retries), Contract‑2 errors (4xx terminal vs 5xx retryable).
6. **Trust posture:** publisher chooses `none|ask|automated`, **default `ask`** (HIL approval per call) via registry restricted‑tools + `agent_tool_access`; xAgent enforces fail‑closed.
7. **Tenant ownership:** `flow_tools` RLS + registry RLS; every publish/editor/invoke checks the tenant from the JWT only. A tenant can never see/publish another tenant's tool or reach another tenant's runtime.
8. **Supply‑chain / secrets:** hardened base image pinned + scanned (Trivy in CI, matching the platform's Tools roadmap); secrets only as Doppler/KMS refs.

---

## 9. Verification (end‑to‑end)

- **Unit/contract:** bridge tests mirror `tool-web-search` (auth 401/403, fine‑scope, idempotency replay, 422 JSON‑Pointer, 10 MiB 413, Contract‑2 shapes) + new: manifest‑builder (form→schema), nodered_adapter (respx‑mocked Node‑RED: 2xx/4xx/5xx/timeout mapping), publish (respx‑mocked registry: create vs version, access), RLS cross‑tenant deny.
- **Manifest validity:** generated manifests validate against `contracts/mcp/manifest.schema.json` (`cd contracts && npm run validate`).
- **Live single‑service:** `uv run python -m tool_flow_bridge`; hand‑seed a binding + Node‑RED flow; `curl /w/<slug>/manifest` and `/w/<slug>/mcp/v1/invoke` → confirm proxied result + idempotency replay header.
- **Full compose E2E (the real gate):** `docker compose up` (auth, tool-registry, tool-flow-bridge, dev Node‑RED, xagent, frontend). Then: (1) log in as a tenant, open `/tools/builder`, build `http in → function(sum) → http response`; (2) Publish with an input form `{a:number,b:number}`; (3) confirm it appears in `GET /bff/api/tools/v1/tools` and in the `/tools` catalog; (4) run an agent task that must call the tool → verify (default `ask`) a HIL approval fires, approve, and the agent gets the computed result; (5) confirm a metering event on `cypherx.agent.tools.invocation.metered`; (6) re‑publish → new version resolves; (7) unpublish → tool disappears from discovery.
- **Security checks:** a flow attempting `http request` to `http://auth-service:8080/...` or `exec`/node‑install is blocked (NetworkPolicy/palette); cross‑tenant `GET /w/<other-slug>/...` is denied by scope/ownership.
- **Regression:** `contracts` `npm test`; existing registry/xAgent tests unaffected (no changes to those services).

Use the platform `verify`/`run` skills to drive the compose stack.

---

## 10. Open items / follow‑ups (post‑MVP within "full production")

- Idle scale‑to‑zero + cold‑start UX for per‑tenant Node‑RED (KEDA/Knative).
- Curated node‑palette catalog + per‑node license vetting + Trivy image scan gate.
- Optional engine‑swap adapters (Elsa/Langflow) behind the same `nodered_adapter` interface, to prove engine‑agnosticism.
- Output `>10 MiB` S3 `output.ref` offload (mirrors tool‑web‑search's planned enhancement) if flows return large payloads.
- Editor autosave/versioning UX; flow templates library seeded with a "New Tool" starter (pre‑wired `http in`/`http response`).
