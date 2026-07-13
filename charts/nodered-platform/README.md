# nodered-platform — the singleton platform (public) Node-RED runtime (Phase 5 · 5-bridge)

This directory holds the manifests for the **one** platform-owned Node-RED instance that hosts
**public** (promoted) tools. It is the egress-ALLOW counterpart to the per-tenant, egress-DENY
runtimes the Flow-Tool-Bridge's `KubernetesProvisioner` creates at request time (`charts/nodered-tenant`).

| Manifest | Purpose |
|----------|---------|
| `manifests/nodered-platform.yaml` | The singleton `Deployment` (replicas: 1, hardened non-root / RO-rootfs / drop-ALL) + `PersistentVolumeClaim` + `Service`. Same image as the tenant runtimes; mounts the platform Secret via `envFrom`. |
| `manifests/nodered-platform-netpol.yaml` | The **egress-ALLOW** `NetworkPolicy`: DNS + the public internet **minus** the metadata endpoint and all internal/RFC-1918 ranges, so it reaches external search providers but never internal services. Ingress: the bridge only. |
| `manifests/nodered-platform-secrets.dopplersecret.yaml` | Doppler-synced `nodered-platform-secrets` Secret: admin/invoke/credential secrets **plus the platform PROVIDER KEY(s)** (SerpAPI / Brave) that public flows use. |
| `manifests/nodered-platform-secrets.example.yaml` | Reference template for the above Secret (never committed with real values; excluded from the ArgoCD app). |

## The public `web_search` tool (drop-in for tool-web-search)

The first public tool hosted on this runtime is **`web_search`** — the flow-tool replacement for the
bespoke `Tools/tool-web-search` service. The importable flow is a **versioned, packaged asset**:
`Tools/tool-flow-bridge/src/tool_flow_bridge/assets/web_search_flow.json` (shipped in the bridge
wheel so the bootstrap can deploy it via the Node-RED Admin API without a chart checkout).

- **Shape:** `http in` (POST `/web_search`) → `prepare` (parse args, pick provider) →
  `switch` → **provider branch** (`build request` → `http request` → `normalize`) **or** **keyless
  mock branch** → `http response`.
- **Contract (identical to tool-web-search):** input `{query:string (required), count?:integer 1..20}`
  (`max_results` accepted as a drop-in alias); output `{results:[{title,url,snippet,rank}]}`.
- **Provider key wiring:** the `normalize`/`build request`/`prepare` function nodes read
  `SEARCH_PROVIDER`, `SERPAPI_API_KEY`, `BRAVE_SEARCH_API_KEY` from the runtime's **own env**
  (delivered by `nodered-platform-secrets` via `envFrom`) using `env.get(...)`. With no provider key
  configured it returns **deterministic keyless MOCK** results (the local/dev default), exactly like
  tool-web-search's keyless fixtures. Tenant runtimes never see these keys — only this egress-ALLOW
  platform runtime does.

### Bootstrap it end-to-end (operator step)

The bridge ships a one-shot bootstrap that publishes + promotes `web_search` to Public:

```bash
# From a shell with the bridge's runtime env (DB, registry, Node-RED admin token, platform runtime):
export BOOTSTRAP_TENANT_ID=<a real tenant uuid>
export BOOTSTRAP_AGENT_ID=<that tenant's agent uuid>
export BOOTSTRAP_USER_JWT=<agent JWT carrying tool:admin + tenant:admin + platform:admin>
python -m tool_flow_bridge.services.bootstrap
```

It (1) deploys `web_search_flow.json` into the runtime, (2) publishes it as an atomic tool +
auto-singleton MCP (`Publisher.create_tool`), then (3) **promotes** it to Public
(`Publisher.promote_mcp`) — re-homing the flow onto THIS platform runtime and registering it under
the platform namespace as `mcp-web-search` (`visibility=public`) via `registry_client.register_platform`.
Full operator runbook: `Tools/tool-flow-bridge/docs/web-search-public-tool.md`.

## How it fits the promote flow
`POST /v1/mcps/{id}/promote` (`platform:admin`) — the sole path to Public — does:
1. **ensure** this platform runtime (the bridge's `ensure_platform_runtime` provisions/records it);
2. **re-home** each member's Node-RED flow onto it (copy via the Admin API, repoint the tool rows'
   `runtime_id` / `node_red_flow_id`) — the http-in URL path is preserved so `http_path` is unchanged;
3. **register** the MCP under the platform (public) namespace in the Tool Registry
   (`tenant_id NULL`, `visibility=public`);
4. **retire** the OLD tenant `server_name` so the stale entry is de-registered.

The bridge can ALSO provision this instance itself at runtime (`PROVISIONER_MODE=kubernetes` →
`services/provisioner.py::_render_platform_objects`) — these manifests are the GitOps-managed
equivalent so the singleton can be deployed declaratively instead.

## Schema / runtime model
- The platform runtime is recorded in `flow_tools.tenant_runtimes` as a **sentinel row** (the nil
  UUID `00000000-0000-0000-0000-000000000000`, never a real tenant) so `flow_tools.tools.runtime_id`
  (FK → `tenant_runtimes`) can be repointed onto it on re-home. Migration
  `db/migrations/20260712_0006__platform_runtime.sql` makes that row readable in every context
  (shared public infrastructure) and writable only in platform (empty-GUC) context.
- Lives in the `cypherx-tools` namespace — reuse the namespace + baseline default-deny from
  `charts/nodered-tenant`.

Apply order: `charts/nodered-tenant` (namespace + default-deny) → `nodered-platform-secrets`
(DopplerSecret) → `nodered-platform.yaml` → `nodered-platform-netpol.yaml`.

> Live-cluster note: the actual deploy (applying these into a running cluster, provider-key wiring,
> and cross-tenant public invocation) is the operator's step; this repo carries the full code +
> manifests. See the Phase-5 report for what can only be validated against a live cluster.
