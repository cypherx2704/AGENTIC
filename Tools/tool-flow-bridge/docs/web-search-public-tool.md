# The public `web_search` flow-tool (Phase 5 · 5-websearch)

`web_search` is the **flow-tool replacement** for the bespoke `Tools/tool-web-search` service. Instead
of a dedicated FastAPI microservice, it is a Node-RED flow hosted on the **singleton platform (public)
Node-RED runtime** and exposed as a **Public** MCP tool through the Flow-Tool-Bridge — so agents
discover and invoke it exactly as before. The bespoke `tool-web-search` service has now been
**decommissioned** and removed from the monorepo (service directory, compose service, registry platform
seed, ECR/Doppler entries) — this flow-tool is its replacement. Follow the cutover ordering below so web
search is never unavailable during the switchover.

## Contract (a drop-in for tool-web-search)

| | tool-web-search | `web_search` flow-tool |
|---|---|---|
| Tool name | `web_search` | `web_search` |
| Input | `{query:string (required), max_results?:integer 1..20}` | `{query:string (required), count?:integer 1..20}` — **`max_results` accepted as an alias** |
| Output | `{results:[{title,url,snippet,rank}]}` | `{results:[{title,url,snippet,rank}]}` |
| Providers | `mock` (keyless, default) · `serpapi` · `brave` | same — selected from the platform runtime env |
| Keyless mode | deterministic canned results | deterministic canned results (mock branch) |

The public param is named **`count`** (also Brave's native param) per the platform contract; the flow
also reads `max_results` so callers of the old service keep working. Provider mappings match
tool-web-search exactly: SerpAPI `organic_results[].{title,link,snippet,position}`, Brave
`web.results[].{title,url,description}`.

## Where the flow lives

`src/tool_flow_bridge/assets/web_search_flow.json` — a **versioned, packaged** Node-RED single-flow
object (shipped in the bridge wheel; loadable via `tool_flow_bridge.services.bootstrap.load_web_search_flow()`).
Shape: `http in` (POST `/web_search`) → `prepare` → `switch(provider|mock)` →
`[build request → http request → normalize]` **or** `[mock]` → `http response`. It satisfies the
publish path's tool-shape rule (exactly one enabled `http in` reachable to an `http response`).

## Provider-key wiring (no key in the bridge)

The flow's function nodes read the provider config from the **platform runtime's own env** via
`env.get(...)`:

- `SEARCH_PROVIDER` = `serpapi` | `brave` (unset ⇒ keyless mock)
- `SERPAPI_API_KEY`, `BRAVE_SEARCH_API_KEY` (alias `BRAVE_API_KEY`)
- optional `SERPAPI_BASE_URL`, `BRAVE_BASE_URL` overrides

These are delivered by the `nodered-platform-secrets` Secret (`envFrom`) — see
`charts/nodered-platform/manifests/nodered-platform-secrets.example.yaml`. The **bridge** holds no
provider key (nothing added to `core/config.py`/`secrets.py`); the egress-ALLOW platform runtime is
the only place the key lives, and tenant runtimes never see it.

## Operator runbook (deploy → load → bootstrap → verify)

1. **Deploy the platform runtime.** Apply `charts/nodered-platform` (namespace + default-deny from
   `charts/nodered-tenant`, then `nodered-platform-secrets` → `nodered-platform.yaml` →
   `nodered-platform-netpol.yaml`). Or run the bridge with `PROVISIONER_MODE=kubernetes` and let
   `ensure_platform_runtime` provision it. In local/compose (`PROVISIONER_MODE=static`) the shared dev
   Node-RED doubles as the platform runtime.

2. **Load + publish + promote (one command).** With the bridge's runtime env available (Postgres,
   Tool Registry, Node-RED admin token, platform runtime reachable):

   ```bash
   export BOOTSTRAP_TENANT_ID=<a real tenant uuid>
   export BOOTSTRAP_AGENT_ID=<that tenant's agent uuid>
   export BOOTSTRAP_USER_JWT=<agent JWT with tool:admin + tenant:admin + platform:admin>
   python -m tool_flow_bridge.services.bootstrap
   ```

   This runs `bootstrap_web_search`, which:
   1. **deploys** `web_search_flow.json` into the tenant runtime (`Publisher.deploy_flow` → Admin API
      `create_flow`);
   2. **publishes** it as an atomic tool + auto-singleton MCP (`Publisher.create_tool`, snake_name
      `web_search`, input `{query, count?}`);
   3. **promotes** the singleton MCP to Public (`Publisher.promote_mcp`) — ensures the platform
      runtime, **re-homes** the flow onto it (so `tools.runtime_id` = the platform runtime and
      `node_red_flow_id` = the deployed flow), registers it under the platform namespace as
      **`mcp-web-search`** (`visibility=public`, `tenant_id NULL`) via
      `registry_client.register_platform`, and retires the old tenant `server_name`.

   > Why publish-then-promote and not a direct platform insert: the `flow_tools.tools`/`mcps`/`mcp_tools`
   > RLS **write** policies require `tenant_id = app.tenant_id`, so rows can only be inserted in a
   > matching **tenant** context (the empty-GUC platform context is read-only for these tables).
   > `promote_mcp` is the sanctioned, fully-tested path to `visibility='public'`, and it re-homes the
   > flow onto the platform runtime — satisfying "deploy the flow into the platform runtime". The
   > source tab left on the tenant runtime is a harmless artifact; delete it post-bootstrap if desired.

3. **Verify an agent can call it.** In the Tool Registry the public server `mcp-web-search` (tool
   `web_search`) is now discoverable to every tenant (cross-tenant public read, migration 0007). An
   agent invokes it via the bridge wire `POST /m/mcp-web-search/mcp` (`tools/call` name `web_search`,
   `arguments {query, count?}`). With no provider key set it returns deterministic mock results; with
   `SEARCH_PROVIDER` + a key set it returns live provider results in the same shape.

## Cutover / decommission ordering (do NOT skip)

The `tool-web-search` removal (service directory, compose service, `TOOL_WEB_SEARCH_BASE_URL`, the
registry platform seed via migration `20260712_0008`, ECR/Doppler entries) must take effect **only after**
the `web_search` Public flow-tool is live — otherwise web search is unavailable in the gap. Operator order:

1. **Deploy** the platform runtime + bootstrap the `web_search` Public flow-tool (steps 1–2 above) so the
   public server `mcp-web-search` exists in the Tool Registry (`visibility=public`, `tenant_id NULL`).
2. **Verify** a cross-tenant agent can discover **and invoke** `web_search` via
   `POST /m/mcp-web-search/mcp` (step 3 above) — returning results in the `{results:[{title,url,snippet,rank}]}`
   shape.
3. **Only then** ship the `tool-web-search` removal: on the next deploy, apply registry migration
   `20260712_0008__decommission_tool_web_search.sql` (retires the old platform seed) and roll out the
   compose/chart/ECR changes that drop the `tool-web-search` service.

Because the replacement is registered and verified first, discovery of `web_search` never lapses; the old
`tool-web-search` platform-seed row is retired in the same deploy that removes the service.

## Live-cluster note

The actual deploy (applying the chart, wiring real provider keys in Doppler, and a cross-tenant public
invocation against a running platform runtime) is the operator's step. This repo carries the full
code + manifests + the packaged flow; the bootstrap orchestration and the flow shape are unit-tested
(`tests/test_web_search_flow.py`, `tests/test_bootstrap_web_search.py`) against fakes.
