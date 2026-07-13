# Flow‑Tool‑Builder (visual no‑code → MCP tool)

Full plan: **[IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)**

## TL;DR
Let customers visually build a workflow and click **Publish** to turn it into an MCP tool that agents auto‑discover — no hand‑written MCP JSON.

**Engine: Node‑RED (Apache‑2.0), not n8n.** n8n's Sustainable Use License forbids embedding its editor in a paid multi‑tenant product without a ~$50k/yr Embed license. A license‑verified survey (reading each project's actual `LICENSE`) left only three cleanly‑permissive, embeddable engines — Node‑RED (Apache‑2.0), Elsa (MIT), Langflow (MIT) — and Node‑RED wins for CypherX (Node.js stack fit, maturity, best mount‑as‑library white‑label, native `HTTP In → HTTP Response` synchronous trigger).

## Architecture in one line
A new **`tool-flow-bridge`** service (fork of `Tools/tool-web-search`) fronts per‑tenant Node‑RED instances: it publishes each workflow as its own `tool-<slug>` server in the **existing Tool Registry** and routes agent invocations (`/w/<slug>/mcp/v1/invoke`) to the tenant's Node‑RED HTTP‑In endpoint. Node‑RED is **only** an execution backend + editor — never the registry, MCP server, or source of truth. The execution seam is a single HTTP‑trigger **adapter**, so the engine is swappable.

## Scope
Full production build: per‑tenant isolation (instance‑per‑tenant + egress‑deny NetworkPolicy + palette/sandbox lockdown), k8s/Helm/GitOps, and a compose end‑to‑end path. Published tools default to **`ask`** (HIL approval); the publisher can choose `none|ask|automated`.

> Folder retains the original name `n8n-implementation`; rename to `flow-tool-builder` if desired.
