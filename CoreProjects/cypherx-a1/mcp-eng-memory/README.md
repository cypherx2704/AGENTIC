# mcp-eng-memory — CypherX MCP server for engineering memory

> A **stateless** Contract-4 MCP server that exposes read-only, source-cited engineering-memory queries by proxying to the `cypherx-a1` product API. No DB, no Kafka, no outbox. Part of [cypherx-a1](../). Owning design: [../docs/09-mcp-server-design.md](../docs/09-mcp-server-design.md).

## Tools (all read-only, idempotent, source-citing)
| Tool | Input | Backed by |
|---|---|---|
| `who_owns` | `{target}` | `/v1/graph/who-owns` |
| `why_built` | `{feature}` | `/v1/graph/why-built` |
| `what_breaks_if_changed` | `{target, max_hops?}` | `/v1/graph/what-breaks` |
| `experts_on` | `{topic}` | `/v1/graph/experts` |
| `graph_neighbors` | `{target, max_hops?}` | `/v1/graph/neighbors` |
| `incident_root_cause` | `{incident}` | `/v1/copilot/ask` (LLM) |
| `how_does_x_work` | `{topic}` | `/v1/copilot/ask` (LLM) |

The canonical manifest is [`manifest.json`](manifest.json) (validates against `contracts/mcp/manifest.schema.json`).

## Endpoints (Contract 4 + 7)
- `POST /mcp` — real MCP (JSON-RPC 2.0 / Streamable HTTP): `initialize` / `tools/list` / `tools/call {name, arguments}`. A `tools/call` result's `structuredContent` is `{output, citations, duration_ms, trace_id}`.
- `GET /manifest` — Contract-4 manifest with strong `ETag` + `If-None-Match` → 304.
- `GET /livez` / `GET /readyz` / `GET /metrics`.

## Auth
Dual-mode (Contract 1/12): **EXTERNAL** bare/api-key-exchanged agent JWT, or **INTERNAL** service token + `X-Forwarded-Agent-JWT` (`on_behalf_of == forwarded agent_id`). Requires coarse `tool:invoke` + fine `tool:mcp-eng-memory:invoke`. The resolved agent JWT is forwarded to the cypherx-a1 backend, which re-verifies it (incl. revocation) and enforces tenant RLS — so this facade stays stateless.

## Run
```bash
uv sync
export CYPHERXA1_BASE_URL=http://localhost:8093
python -m mcp_eng_memory          # host PORT (8080 in image; host map 8094 in compose)
uv run pytest && uv run ruff check .
```

## Invariants
- **Stateless** — never add a DB/Kafka/outbox. Per-invocation metering is the calling agent's (xAgent) outbox, not this server's.
- Identity from JWT only; the body carries only `tool`/`args`. 1 MiB request / 10 MiB output caps.
