# tool-flow-bridge

Turns **visually-built Node-RED workflows into Contract-4 MCP tools**. It is the backend of
the CypherX **Tool Builder**: a user builds a flow in the embedded editor, clicks *Publish*,
and this service registers it as an MCP tool in the existing **Tool Registry** — so any agent
can immediately discover and call it. Node-RED is **only** the execution backend + editor;
this service owns tool identity, schema, tenancy, and the MCP wire protocol.

## Two surfaces
- **Control plane** (`/v1/*`, called by the user via the BFF): `POST /v1/flow-tools` (publish /
  re-publish), `GET /v1/flow-tools`, `GET/DELETE /v1/flow-tools/{slug}`, `GET /v1/flows`
  (workflow picker), `POST /v1/editor-sessions` + `GET /v1/editor-runtime` (editor embed).
- **MCP runtime** (called by xAgent): `GET /w/<slug>/manifest`, `POST /w/<slug>/mcp/v1/invoke`
  (`{"tool","args"}` → `{"tool","result"}`), plus `/livez` `/readyz` `/metrics`.

## Flow
```
Publish: analyze Node-RED flow (http in → http response) → generate Contract-4 manifest
         → upsert binding (flow_tools DB, RLS) → register in Tool Registry (Contract-12
         service token + forwarded user JWT) → set access (publisher picks; default 'ask').
Invoke:  xAgent → /w/<slug>/mcp/v1/invoke → auth + scope + idempotency + schema-validate
         → POST the tenant's Node-RED HTTP-In endpoint → JSON result.
```

## Engine-agnostic
Execution is behind one adapter (`services/nodered_adapter.py`). Swap it (Elsa / Langflow /
any HTTP-triggerable engine) and nothing else on the MCP/registry/invoke path changes.

## Build, test, run
```bash
uv sync
./.venv/Scripts/python.exe -m pytest -q          # 32 tests, no live infra
./.venv/Scripts/python.exe -m ruff check src tests
./.venv/Scripts/python.exe -m tool_flow_bridge   # serves on PORT (default 8000 host / 8080 container)
```
Full stack: `infra/compose/docker-compose.yml` (services `tool-flow-bridge` host 8096, `nodered`).
Run migrations first: `docker compose --profile migrate up migrate` (creates schema `flow_tools`
+ role `flow_tools_user`).

## Tenancy & provisioning
`PROVISIONER_MODE`: `static` (one shared dev Node-RED — compose), `kubernetes` (one hardened
instance per tenant with an egress-deny NetworkPolicy — `services/provisioner.py` +
`charts/nodered-tenant/`), or `docker`. See `charts/tool-flow-bridge/` for the Helm consumer.

## Config
Env only (pydantic-settings, no prefix). See `.env.example`. Owns Postgres schema `flow_tools`
(role `flow_tools_user`, Neon POOLED). Registers tools via `TOOL_REGISTRY_URL`; mints service
tokens via `AUTH_SERVICE_URL` + `SERVICE_BOOTSTRAP_SECRET`. No Kafka; per-invocation metering is
xAgent's outbox, never this service.
