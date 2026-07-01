# CypherX Tool Registry (WP11, part 1)

The Tool Registry is the source of truth for the MCP tools an agent can discover and
invoke. It holds the **tool/version registry**, resolves **tenant-vs-platform
discovery** (with tenant-priority shadowing + version pinning), validates and stores
**Contract-4 manifests**, declares each tool's **capabilities/scopes**, and tracks
each tool's **health** via a background manifest poll.

Python 3.12 · FastAPI · uv · psycopg3 (async) · structlog · Prometheus.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/v1/tools` | any authenticated principal | UNION of platform + the caller's tenant tools (tenant shadows platform), each with resolved manifest, invoke URL, required scopes, health. |
| GET  | `/v1/tools/{name}` | any authenticated principal | One tool by name (tenant shadows platform); `?version=` pins a specific active version, else latest active. |
| POST | `/v1/tools` | `tool:admin` / `platform:admin` | Register a NEW tenant tool + first version from a Contract-4 manifest. |
| POST | `/v1/tools/{name}/versions` | `tool:admin` / `platform:admin` | Append a version; retention keeps **max 3** active versions (oldest retired). |
| GET  | `/livez` `/readyz` `/metrics` | none | Health (Contract 7) + Prometheus. |

Errors use the Contract-2 envelope (`{ "error": { code, message, details?, request_id, trace_id, timestamp } }`).
Auth is dual-mode (Contract 1/12/13): a bare agent JWT, OR a service token +
`X-Forwarded-Agent-JWT` with matching `on_behalf_of`. The WP03 verifier-side revocation
mirror (jti / kid / agent-epoch via shared Valkey) runs after signature checks and
**fails open** if Valkey is down.

## Schema (`tools` schema)

| Table | Purpose |
|-------|---------|
| `tools` | tool registry: `tool_id, tenant_id (NULL=platform), name, status, latest_version, created_at`. |
| `tool_versions` | version chain: `tool_id, version, manifest JSONB, status (active\|retired)`; max 3 active retained. |
| `tool_capabilities` | declared scopes/capabilities per tool: `capability, required_scope`. |
| `tool_health` | manifest poll state: `status (active\|degraded\|offline), last_etag, consecutive_failures, last_polled`. |

### RLS — closing the marketplace hole

Every tenant-scoped table has a **split** policy instead of one permissive `FOR ALL`:
a `*_read` SELECT policy (`USING own OR platform`) for the discovery UNION, and a
`*_write` policy whose **`WITH CHECK (tenant_id = current_tenant)`** rejects any
INSERT/UPDATE naming another tenant's id (or `NULL` to forge a platform tool) — applied
to **every** table including `tool_capabilities`. A third `*_platform` policy (gated on
an empty `app.tenant_id`) lets the seed/poller manage platform rows without ever
touching a tenant's rows. All predicates use
`NULLIF(current_setting('app.tenant_id', true), '')::uuid` (pooled-reset safe). See
`db/migrations/README.md`.

## Discovery resolution

1. RLS returns the UNION of the caller's tenant rows + platform rows.
2. **Tenant priority (shadowing):** for a given name, the tenant's tool hides a platform
   tool of the same name.
3. **Version pinning:** resolve the requested `?version=` (must be active) or the latest
   active version; return its manifest + resolved `invoke_url` (manifest `base_url`, else
   `http://<name>:8080`) + required scopes.

## Health poll state machine

Each tool exposes `GET {base_url}/manifest`. The registry polls it **eagerly at
registration** and from a **30s background sweep**, sending `If-None-Match` with the
cached ETag:

- `200` → **active**, cache the new manifest + ETag.
- `304` → **active** (unchanged; ETag preserved).
- error / timeout / non-2xx → **failure**.

Consecutive failures drive `active → degraded` (after `HEALTH_DEGRADE_AFTER`) →
`offline` (after `HEALTH_OFFLINE_AFTER`); a single success resets to `active`. Fail-soft:
a poll error never escapes the loop. The poll logic is decoupled from HTTP behind a
small client protocol so it is fully unit-testable with a fake client.

## Platform seed

`tool-web-search` is seeded as a platform tool (tenant_id NULL) at startup
(idempotent), with its `web_search` capability + `tool:tool-web-search:invoke` scope.
Its manifest `base_url` comes from `TOOL_WEB_SEARCH_BASE_URL` (never hardcoded).

## Run

```bash
python -m uv venv
python -m uv sync
# tests (no live infra needed — fakes + db_pool=None degradation):
./.venv/Scripts/python.exe -m pytest -q

# locally against the dev stack:
PORT=8088 DATABASE_URL=postgresql://tool_user:localdev@localhost:5432/cypherx_platform \
  PGOPTIONS='-c search_path=tools,public' \
  AUTH_JWKS_URL=http://localhost:8080/.well-known/jwks.json \
  TOOL_WEB_SEARCH_BASE_URL=http://localhost:8087 \
  ./.venv/Scripts/python.exe -m tool_registry
```

Migrations: apply `db/migrations/*.sql` (superuser) in order. Container: multi-stage
`Dockerfile` (uv, non-root, stdlib `/livez` healthcheck), `python -m tool_registry` on
`PORT=8080`.

## Config

All settings are env-overridable (`core/config.py`); see `.env.example`. Nothing is
hardcoded at a call site — timeouts, the retention cap, failure thresholds, the seed
base URL, and the discovery row cap are all configuration.
