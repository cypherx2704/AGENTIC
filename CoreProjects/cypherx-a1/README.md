# cypherx-a1 — Autonomous Engineering Memory

> Ingest your engineering history (GitHub first) → a tenant-scoped knowledge **graph** + **RAG** corpus → LLM knowledge-extraction → a **cited** hybrid-retrieval **copilot** → an **MCP server** so AI coding agents can ask *"who built this / who owns this / what breaks if I change X / why was this decided"* — with sources.

A first-class CypherX **consuming app** (peer of `xAgent/ax-1`) built on the SharedCore services (auth, llms, guardrails, rag, memory). All business logic lives here; SharedCore stays generic. See [CLAUDE.md](CLAUDE.md) for the engineering guide and [docs/](docs/) for the full product-development documentation.

## Why
Senior engineers leave, docs go stale, knowledge hides in PRs/Slack/incidents. cypherx-a1 continuously builds a *living* engineering memory from the systems of record and answers questions about it with citations — and exposes that memory to autonomous coding agents over MCP.

## Console (UI-1)
A self-contained **Engineering Memory Console** ships with the service at **http://localhost:8093/ui** (same-origin; paste an agent JWT). It implements Ask (cited copilot), Graph lenses (who-owns / what-breaks / experts / why-built / neighbors), the Activity timeline ("what changed, who, when"), Entity detail, and a live Citations rail. Full UI/UX design (all screens + every setting) is in [docs/ui-design.md](docs/ui-design.md); production folds these screens into the platform Console behind the BFF (no token in the browser).

## Architecture (three layers)
1. **Product service** (`src/cypherx_a1`, FastAPI) — owns the `cypherx_a1` Postgres schema (the knowledge graph), the connector ingestion pipeline, the LLM extraction engine, and the hybrid retrieval orchestrator.
2. **Copilot** — `POST /v1/copilot/ask`: memory recall → guardrails(in) → hybrid retrieve → llms → guardrails(out) → cited answer.
3. **MCP facade** (`mcp-eng-memory/`) — a stateless Contract-4 server proxying the graph/copilot API as MCP tools (`who_owns`, `what_breaks_if_changed`, `experts_on`, `why_built`, …).

```
GitHub/Jira/Slack ─▶ Connector SPI ─▶ landing(raw) ─▶ normalize(graph) ─▶ extract(LLM via llms-gateway)
                                                      │                     │
                                          app Postgres (graph)      RAG KBs (vectors)        Memory (chat)
                                                      └──────────── hybrid retrieve (RRF) ───────────┘
                                                                          │
                                            guardrails ── llms ── cited answer ──▶ REST + MCP
```

## Quickstart (keyless, local compose)
From `infra/compose/` (external Neon; fill the `CYPHERXA1_DATABASE_URL` + `CYPHERXA1_DB_PASSWORD` in `.env`):
```bash
docker compose --profile migrate up migrate              # creates schema cypherx_a1 + role + auth ACL edges
docker compose up -d --build cypherx-a1 mcp-eng-memory   # + its deps (auth/llms/guardrails/rag/memory)
```
Then (with an agent JWT minted via the BFF/auth — scope `cypherxa1:ingest`/`cypherxa1:query` or `agent:execute`):
```bash
# 1) Ingest the bundled sample repo (keyless fixtures)
curl -XPOST localhost:8093/v1/connectors/github/sync -H "Authorization: Bearer $JWT" -d '{}'
# 2) (optional) run the LLM extraction pass
curl -XPOST localhost:8093/v1/extract -H "Authorization: Bearer $JWT"
# 3) Ask the copilot
curl -XPOST localhost:8093/v1/copilot/ask -H "Authorization: Bearer $JWT" \
  -d '{"question":"Who owns acme/payments and what breaks if I change auth-service?"}'
# 4) Or query directly (also what the MCP server proxies)
curl -XPOST localhost:8093/v1/graph/what-breaks -H "Authorization: Bearer $JWT" -d '{"target":"auth-service"}'
# 5) MCP invoke (real MCP: JSON-RPC 2.0 tools/call)
curl -XPOST localhost:8094/mcp -H "Authorization: Bearer $JWT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"who_owns","arguments":{"target":"acme/payments"}}}'
curl localhost:8094/manifest
```

## Endpoints
| Method / path | Purpose | Scope |
|---|---|---|
| `POST /v1/copilot/ask` | Cited LLM answer over the engineering memory | `cypherxa1:query` |
| `POST /v1/graph/who-owns` \| `/what-breaks` \| `/experts` \| `/why-built` \| `/neighbors` | Read-only cited graph queries | `cypherxa1:query` |
| `POST /v1/connectors/{kind}/sync` | Backfill/sync a source into the graph + RAG | `cypherxa1:ingest` |
| `POST /v1/extract` | Run the LLM knowledge-extraction pass | `cypherxa1:ingest` |
| `POST /webhooks/{kind}?tenant=<uuid>` | App-owned webhook receiver (signature-verified) | — |
| `GET /livez` \| `/readyz` \| `/metrics` | Contract-7 health | — |
| `POST /mcp` (JSON-RPC 2.0) \| `GET /manifest` (port 8094) | MCP tool surface | `tool:invoke` + `tool:mcp-eng-memory:invoke` |

## Develop
```bash
uv sync && uv run pytest && uv run ruff check src tests && uv run mypy
```
See [docs/16-testing-strategy.md](docs/16-testing-strategy.md) and [docs/14-build-plan-and-phasing.md](docs/14-build-plan-and-phasing.md).
