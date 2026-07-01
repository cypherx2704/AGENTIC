# cypherx-a1 — UI / UX Design

> A complete design for the **Engineering Memory Console**: a human web UI over cypherx-a1's REST + MCP backend, covering every feature (copilot, graph queries, activity timeline, ownership/expertise, connectors, extraction & consolidation, knowledge bases, MCP tools, observability) and **every setting**. Backend is headless today (API `:8093` + MCP `:8094`); this is the design for the human surface. Grounded in the real endpoints + `core/config.py`.

---

## 1. Goals, audience, principles

**Who uses it**
- **Engineers** (primary) — ask "who owns X / what breaks / what changed / why was this built", browse ownership + activity.
- **Tech leads / EMs** — expertise maps, ownership concentration (bus-factor risk), activity over time.
- **Platform admins** — connectors, extraction/consolidation runs, settings, health/cost, MCP keys for AI agents.

**Principles**
1. **Cited by default** — every answer/result shows its provenance (PR/commit/ticket/person), one click to the source. The graph is the crown jewel; the UI makes provenance visible everywhere.
2. **Answer-first, then explore** — the copilot is the front door; results deep-link into the graph/activity/expertise views.
3. **Time is first-class** — the graph is bitemporal; the UI surfaces "as of" / "what changed when" prominently.
4. **Trust signals** — confidence badges, recency, "flagged/low-confidence", "superseded" are shown, not hidden.
5. **Safe by construction** — no token in the browser (BFF holds it); read-only is the default; mutating actions (sync/extract/consolidate, settings) are gated by scope + confirm.
6. **No over-engineering** — one console, progressive disclosure; admin/settings tucked behind a clear boundary.

---

## 2. Information architecture

```
Engineering Memory Console
├── Ask (Copilot)                 ← default landing
├── Explore
│    ├── Graph                    ← entity search + neighborhood graph
│    ├── Activity                 ← "what changed, who, when" timeline
│    ├── Ownership & Expertise    ← who owns / experts / bus-factor
│    └── Entity detail            ← drill-in (bitemporal, edges, citations)
├── Knowledge
│    ├── Connectors               ← GitHub/Jira/… install, sync, webhooks
│    ├── Extraction & Memory      ← run extraction / consolidation, jobs, cost
│    └── Knowledge bases (RAG)    ← KBs, pinned embedding model, status
├── Agents & Tools (MCP)          ← manifest, tool playground, API keys
├── Observability                 ← health, usage/cost, events
└── Settings                      ← all config, grouped (env-managed vs overridable)
```

Top-level nav = left rail (icons + labels, collapsible). Tenant/org switcher + user menu top-right.

---

## 3. Global layout (app shell)

```
┌───────────────────────────────────────────────────────────────────────────────────────┐
│ ◧ cypherx · Engineering Memory      [ ⌘K  Ask anything… ]        ⊙ acme-org ▾   ◑  👤 ▾ │  ← top bar: global ask (⌘K), tenant switch, theme, user
├───────┬───────────────────────────────────────────────────────────────────┬─────────────┤
│ ▸ Ask │                                                                   │  CONTEXT    │
│ ▸ Expl│                      MAIN CONTENT                                  │  / CITATIONS│
│   Grph│                      (route view)                                  │  panel      │
│   Actv│                                                                   │  (collapsible
│   Own │                                                                   │   right rail)│
│ ▸ Know│                                                                   │             │
│ ▸ MCP │                                                                   │             │
│ ▸ Obs │                                                                   │             │
│ ⚙ Set │                                                                   │             │
├───────┴───────────────────────────────────────────────────────────────────┴─────────────┤
│  status: ● DB ok · ● Auth ok · ○ Valkey soft   ·   last sync 4m ago   ·   42 entities    │  ← thin status strip (/readyz + counts)
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- **⌘K global ask** is available on every screen (calls `POST /v1/copilot/ask`).
- **Right "Citations" rail** persists across Explore views — selecting any answer/result populates it with sources.
- **Status strip** polls `/readyz` + entity/edge counts; turns amber if `postgresql: fail` (the current running-container state) with a "DB not provisioned" hint.

---

## 4. Screen designs (wireframes + API mapping)

### 4.1 Ask (Copilot) — landing
`POST /v1/copilot/ask {question, session_id?, top_k}` → `{answer, citations[], used, trace_id, duration_ms}`

```
┌─ Ask ───────────────────────────────────────────────────────────┬─ Citations ──────────┐
│  Conversation: "Payments onboarding"            + New   ⟳ History │  Sources (6)         │
│                                                                  │ ┌──────────────────┐ │
│  🧑 Who owns acme/payments and what breaks if I change           │ │ 🔗 acme/payments  │ │
│     auth-service?                                                │ │   repo · github   │ │
│                                                                  │ │   owner: Alice Ng │ │
│  🤖 Alice Ng owns acme/payments [1]. Changing auth-service would │ ├──────────────────┤ │
│     impact acme/payments (depth 1) [2], which Alice owns…        │ │ 🔗 PR #101        │ │
│     ⟦confidence ●●●●○⟧                                            │ │   "Add Stripe…"   │ │
│     ── used: graph 4 · rag 2 · keyword 3 · 380ms ──              │ │   by Alice · ●●●● │ │
│                                                                  │ └──────────────────┘ │
│  [ Ask a follow-up…                              ] top_k:[8 ▾] ⏎ │  (click → entity)    │
└──────────────────────────────────────────────────────────────────┴──────────────────────┘
```
- Inline citation chips `[1] [2]` ↔ right-rail cards; hover highlights; click opens **Entity detail**.
- `used` + latency shown for transparency (which legs fired).
- Per-message **guardrail** state surfaces if blocked (422 → "This question was blocked by guardrails").
- Conversation memory (`copilot_memory_enabled`) → "History" lists prior sessions (`session_id`).
- Suggested starters: *Who owns…* · *What breaks if I change…* · *Who's the expert on…* · *Why was … built?* · *What changed in … this week?*

### 4.2 Explore › Graph
`POST /v1/graph/who-owns | /what-breaks | /experts | /why-built | /neighbors` (+ entity search)

```
┌─ Graph ──────────────────────────────────────────────────────────────────────────────┐
│  [ 🔍 search entities…  "auth-service" ]   kind:[all ▾]   as-of:[ now ▾ 📅 ]            │
│ ┌────────────── results ──────────────┐ ┌──────────── neighborhood ──────────────────┐ │
│ │ ● auth-service   service   ●●●●○     │ │            (alice)──owns──▶[acme/payments] │ │
│ │ ● acme/payments  repo      ●●●●●     │ │                              │ depends_on   │ │
│ │ ● Alice Ng       person              │ │                              ▼              │ │
│ │ …                                    │ │                        [auth-service]◀──owns─(carol)
│ └──────────────────────────────────────┘ │   hops:[2 ▾]  rels:[all ▾]  ▣ show stale   │ │
│  Quick lenses for the selected node:                                                   │ │
│   [ Who owns ]  [ What breaks if changed ]  [ Experts ]  [ Why built ]  [ Activity ]   │ │
└──────────────────────────────────────────────────────────────────────────────────────┘
```
- **Node-link graph** (force/dagre) of `neighbors`; edge labels = `rel`, thickness/opacity = confidence, dashed = `valid_to` (stale/superseded), badge = recency.
- **"as-of" date picker** drives bitemporal reads ("show the graph as it was on 2026-05-01").
- One-click **lenses** map to the deterministic graph endpoints; results render as cited cards (right rail) + highlight on the graph.
- `▣ show stale` toggles superseded edges (the `supersedes_edge_id` chain) for audit.

### 4.3 Explore › Activity — "what changed, who, when"  *(Phase B headline)*
`POST /v1/graph/activity {target, since?, until?}` → time-ordered cited events

```
┌─ Activity ───────────────────────────────────────────────────────────────────────────┐
│  scope:[ acme/payments ▾]   range:[ last 30 days ▾ 📅 since–until ]   who:[ anyone ▾ ]  │
│                                                                                        │
│  Jun 13  ● ticket  Payment retries fail on 429 from auth-service     — Bob Reyes   🔗   │
│  Jun 13  ● pr      Refactor payment retry with exponential backoff   — Bob Reyes   🔗   │
│  Jun 13  ● pr      Add Stripe webhook handler                        — Alice Ng    🔗   │
│  Jun 12  ◆ change  Tune payment retry exponential backoff            — Bob Reyes   🔗   │
│  Jun 10  ◆ change  Add Stripe webhook signature verification         — Alice Ng    🔗   │
│        … grouped by day · ◆ = commit (granularity), ● = PR/ticket/incident …            │
│  [ Export CSV ]   [ Open as graph ]                                  feeds MCP what_changed │
└────────────────────────────────────────────────────────────────────────────────────────┘
```
- A **vertical timeline** of `change/pr/ticket/incident` nodes attributed to authors, newest first.
- Filter by **scope** (repo or person), **date range** (`since`/`until`), and **who**.
- Switching scope to a person → "what has Bob worked on, when".
- Mirrors exactly what the MCP `what_changed` tool returns — humans + AI agents see the same surface.

### 4.4 Explore › Ownership & Expertise
`who-owns`, `experts`, + the consolidation `expertise_summary` nodes

```
┌─ Ownership & Expertise ────────────────────────────────────────────────────────────────┐
│  for:[ acme/payments ▾]                                                                 │
│ ┌─ Owners ──────────────────────┐ ┌─ Experts (recency-weighted) ─┐ ┌─ Bus-factor ─────┐ │
│ │ Alice Ng   owns        ●●●●●  │ │ Bob Reyes   authored   3.0   │ │  ⚠ concentration │ │
│ │ Carol Sun  owns(svc)   ●●●●○  │ │ Alice Ng    reviewed   1.0   │ │  Alice 62%       │ │
│ └───────────────────────────────┘ └──────────────────────────────┘ │  (Phase C)       │ │
│ ┌─ Expertise summaries (from reflection) ───────────────────────────┐└──────────────────┘ │
│ │ "Expertise: Alice Ng" — Stripe webhooks, signature verification…  │  source=consolidation │
│ │   evidence: PR#101, change c0ffee1, …   ⟳ last consolidated 2h ago │  [ Re-run reflection ]│
│ └───────────────────────────────────────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- **Owners / Experts** columns from the cited graph queries (confidence/score badges).
- **Expertise summaries** = the `expertise_summary` nodes from the consolidation pass, with their evidence edges; "Re-run reflection" → `POST /v1/extract?consolidate=true`.
- **Bus-factor / ownership-concentration** card is a **Phase C** placeholder (clearly marked "coming in Phase C").

### 4.5 Entity detail (drill-in)
`/v1/graph/neighbors` + the entity's edges/citations; bitemporal view

```
┌─ acme/payments · repo ─────────────────────────────────────────────────────────────────┐
│  github · owner Alice Ng · depends_on auth-service, payments-db · 3 PRs · 1 ticket       │
│  ┌ Relationships ─────────────────┐ ┌ Activity (mini) ─────┐ ┌ As-of timeline ─────────┐ │
│  │ owns ◀ Alice (●●●●●, current)  │ │ Jun13 PR#102 Bob     │ │ ▸ now                    │ │
│  │ depends_on ▶ auth-service      │ │ Jun13 PR#101 Alice   │ │ ▸ 2026-05  (owner: …)    │ │
│  │ depends_on ▶ payments-db       │ │ Jun12 change Bob     │ │ ▸ supersede chain (2)    │ │
│  │ ⊘ owns ◀ Dave (superseded 5/1)│ │ …                    │ │   click to inspect       │ │
│  └────────────────────────────────┘ └──────────────────────┘ └──────────────────────────┘ │
│  Citations: PR#101 🔗 · commit c0ffee1 🔗 · CODEOWNERS …                                   │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- **Relationships** list with confidence + current/superseded (the `supersedes_edge_id` chain rendered as an audit trail).
- **As-of timeline** lets you scrub bitemporal history ("who owned this on date Y").

### 4.6 Knowledge › Connectors
`POST /v1/connectors/{kind}/sync {repo?, mode}` · webhook config

```
┌─ Connectors ───────────────────────────────────────────────────────────────────────────┐
│  ＋ Add connector        ( GitHub · Jira* · Slack* · Confluence* · PagerDuty* · CI/CD* )  │
│ ┌ GitHub — acme ─────────────────────────────────────────────────────────────────────┐ │
│ │ status ● active   mode:[ live ▾]   change granularity:[ auto ▾ | commit | pr_ticket] │ │
│ │ repos: acme/payments, acme/api          token: ••••••••  [edit]                       │ │
│ │ webhook: https://…/webhooks/github?tenant=…   secret ••••  [copy] [rotate] [verify]   │ │
│ │ Backfill:  [ Sync now ▾ full | incremental ]   page size [100]     last: 4m ago ✓     │ │
│ │   ↳ 12 records, 9 new, 16 edges, 0 errors                                              │ │
│ └───────────────────────────────────────────────────────────────────────────────────┘ │
│  * connectors marked with * are roadmap (SPI ready, not yet implemented)                  │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- Per-connector card: **mode** (`connector_mode`), **change granularity** (`connector_change_granularity`), secrets (token/webhook secret — write-only, masked), webhook URL builder, **Sync now** (full/incremental → the sync endpoint) with last-run stats.
- "Add connector" reflects the connector SPI; non-GitHub kinds are shown as roadmap.

### 4.7 Knowledge › Extraction & Memory
`POST /v1/extract[?consolidate=true]` + `extraction_jobs` ledger

```
┌─ Extraction & Memory ──────────────────────────────────────────────────────────────────┐
│  [ Run extraction ]   [ Run reflection/consolidation ]   ▣ consolidate with extraction   │
│  extractor v1.0.0 · confidence floor 0.60 [flag▾] · consolidation avg≥0.75, min cluster 3 │
│ ┌ Recent jobs (extraction_jobs) ───────────────────────────────────────────────────────┐ │
│ │ node                          kind     edges  llm_call_id   cost     when              │ │
│ │ PR acme/payments#101          pr        2     —(mock)       $0.000   2m ago   ✓        │ │
│ │ Expertise: Alice Ng           summary   6     call_… 1f3a   $0.002   2m ago   ✓        │ │
│ └────────────────────────────────────────────────────────────────────────────────────┘ │
│  Scheduled reflection: [○ off ▾ daily]  (consolidation_schedule_enabled / interval)       │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- Buttons trigger extraction / consolidation; the **jobs ledger** shows idempotency + **cost** (`llm_call_id`, `cost_usd`) — provenance for spend.
- Surfaces the key extraction/consolidation knobs inline (deep-link to Settings for the rest).

### 4.8 Knowledge › Knowledge bases (RAG)
read-only over `cypherx_a1.rag_kbs`

```
┌─ Knowledge bases (RAG) ────────────────────────────────────────────────────────────────┐
│  Embedding model (pinned): text-embedding-3-small · dim 1536   🔒 immutable per KB        │
│  eng-code           kb_…a1   docs 28   chunks 140   ● ready                               │
│  eng-conversations  kb_…b2   docs 0    chunks 0     ○ empty                               │
│  eng-docs · eng-incidents …                                                              │
│  ⓘ Vectors live in SharedCore RAG; the graph never enters RAG (provenance only via doc_id)│
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- Read-only status; the pinned embedding model is shown 🔒 (changing it = delete+re-ingest, documented). Reinforces the architecture boundary.

### 4.9 Agents & Tools (MCP)
`GET :8094/manifest` · `POST /mcp/v1/invoke`

```
┌─ Agents & Tools (MCP) ─────────────────────────────────────────────────────────────────┐
│  Server: mcp-eng-memory @1.0.0  ·  /manifest ✓  ·  registered in tool-registry ✓          │
│ ┌ Tools (8) ─────────────────────────┐ ┌ Playground ──────────────────────────────────┐ │
│ │ who_owns · why_built · what_breaks…│ │ tool:[ what_changed ▾ ]                       │ │
│ │ experts_on · graph_neighbors       │ │ args { "target":"acme/payments" }            │ │
│ │ what_changed · incident_root_cause │ │ [ Invoke ]   → output + citations (read-only) │ │
│ │ how_does_x_work                    │ └──────────────────────────────────────────────┘ │
│ ├ For external AI agents (Claude Code / Cursor) ─────────────────────────────────────────┤ │
│ │ Endpoint  http://…:8094/mcp/v1/invoke    Manifest  /manifest                            │ │
│ │ API keys  [ ＋ Create key ]  (scopes tool:invoke + tool:mcp-eng-memory:invoke)          │ │
│ │  • coding-bot-key   created 2026-06-13   [revoke]                                        │ │
│ └─────────────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- Lists the 8 tools + their input schemas; a **playground** to invoke them (read-only, cited).
- **API-key management** for external coding agents (create key with the right scopes via Auth → the headline "AI agents query the memory" workflow). Connection snippet for Claude Code / Cursor.

### 4.10 Observability
`/readyz` `/metrics` + usage events

```
┌─ Observability ────────────────────────────────────────────────────────────────────────┐
│ Health  ● postgresql ok · ● auth_jwks ok · ○ valkey soft        (links to /readyz)        │
│ Usage   copilot 142 asks · extraction 38 · consolidation 5 · est. cost $0.42 (this week)  │
│ Pipeline graph: ingested 312 → edges 540 → summaries 11        topics: cypherx.cypherxa1.* │
│ Events (outbox/Kafka) tail · DLQ 0 · last record.normalized 4m ago                        │
│ Metrics: copilot latency p50 380ms / p95 1.2s · downstream calls by service               │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```
- Surfaces `/metrics` (Prometheus) as charts, the usage-recorded events (cost), health, and the eventing/outbox state.

---

## 5. Settings — full coverage (every `core/config.py` knob)

Settings are grouped into tabs. **Editability:** infra/identity values are **env/Doppler-managed** (shown read-only with their source) because the service reads config from env at boot; **tunable** values (models, thresholds, weights, connector behavior) are surfaced as editable controls that write to a **per-tenant overrides** store (a small additive `cypherx_a1.settings_overrides` table read at request time) — this is the one new piece the UI needs and is called out in the roadmap. Each control shows its **default**, **current**, and a **reset**.

| Tab | Setting (config key) | Control | Editable? |
|---|---|---|---|
| **Models & LLM** | `copilot_model`, `extraction_model` | model dropdown (gateway aliases) | ✅ tunable |
| | `copilot_max_tokens`, `copilot_temperature` | number / slider | ✅ |
| | `extraction_max_tokens`, `extraction_temperature`, `consolidation_max_tokens` | number / slider | ✅ |
| **Retrieval & ranking** | `retrieval_graph_limit`, `retrieval_keyword_limit`, `retrieval_max_hops`, `retrieval_context_max_chunks` | number | ✅ |
| | `retrieval_rrf_k` | number (default 60) | ✅ |
| | `rerank_confidence_weight`, `rerank_recency_weight`, `rerank_recency_halflife_days` | sliders (Phase A) | ✅ |
| | `rag_query_top_k`, `rag_query_min_score`, `rag_query_ef_search` | number (clamped to RAG caps) | ✅ |
| **Extraction & memory** | `extractor_version` | text (bump = supersede) | ✅ admin |
| | `extraction_confidence_floor`, `confidence_floor_mode` (flag/drop) | slider + toggle (Phase A) | ✅ |
| | `consolidation_version`, `consolidation_avg_confidence`, `consolidation_min_cluster`, `consolidation_lookback_limit` | number/slider (Phase B) | ✅ |
| | `consolidation_schedule_enabled`, `consolidation_interval_seconds` | toggle + interval | ✅ |
| | `copilot_memory_enabled`, `copilot_memory_type` | toggle + select | ✅ |
| **Knowledge bases** | `rag_embedding_model`, `rag_embedding_dim` | 🔒 read-only (pinned/immutable) | ❌ |
| | `rag_kb_code/conversations/docs/incidents` | read-only labels | ❌ |
| **Connectors** | `connector_mode` (mock/live) | toggle | ✅ |
| | `connector_change_granularity` (auto/commit/pr_ticket) | select (Phase B) | ✅ |
| | `github_token`, `github_webhook_secret` | secret (write-only, masked) | ✅ |
| | `github_api_url`, `backfill_page_size` | text / number | ✅ |
| **Workers & events** | `worker_enabled`, `outbox_publisher_enabled` | toggles | ⚙ env (read-only in UI) |
| | `ingestion_topic_prefix`, `ingestion_consumer_group`, `worker_max_attempts`, `usage_topic`, `kafka_brokers` | read-only | ⚙ env |
| **Connections (infra)** | `database_url` (masked), `valkey_url` | read-only + health dot | ⚙ env/Doppler |
| | `auth_jwks_url`, `auth_issuer_url`, `auth_platform_audience`, `auth_service_url` | read-only | ⚙ env |
| | `service_principal_name`, `service_bootstrap_secret` (masked) | read-only | ⚙ Doppler |
| | downstream URLs + `*_timeout_seconds` (llms/guardrails/rag/memory/tool_registry) | read-only + health | ⚙ env |
| | `revocation_*`, `otel_*`, `environment`, `service_version` | read-only | ⚙ env |
| **Access (Phase C)** | per-repo / per-team `resource_acls` | rules editor (who can read which repo's memory) | ✅ (Phase C enforcement) |

> Honesty note: today the service is env-driven (no runtime config store). The UI's editable settings require the small `settings_overrides` table above (one additive migration + a read-through in `get_settings()`), or integration with the platform control-plane (Phase 11). Until then, editable controls are shown but flagged "applies after the overrides store lands". Env-managed rows are always read-only with their Doppler/env source.

---

## 6. Key components (design system primitives)

- **Citation chip / card** — `{title, source, kind, author, url, confidence, snippet}`; inline `[n]` ↔ right-rail card; click → Entity detail.
- **Confidence badge** — 5-dot `●●●●○` from edge `confidence`; muted when `flagged`/below floor.
- **Recency pill** — "2d ago" / "stale 1y"; drives the rerank intuition.
- **Supersede trail** — `⊘ superseded 5/1 → current` chain (the `supersedes_edge_id` audit).
- **Entity kind icon** — person / repo / service / pr / ticket / incident / change / decision / capability / expertise_summary.
- **Bitemporal "as-of" scrubber** — date control feeding point-in-time reads.
- **Graph canvas** — node-link, edge=`rel`, weight=confidence, dashed=stale.
- **Run-button + job toast** — for sync/extract/consolidate, with live status + result stats.
- **Empty / loading / error states** — every list has a first-run empty state ("No data yet — add a connector and Sync"); errors render the Contract-2 envelope (`code`, `message`, `trace_id`).

---

## 7. Core interaction flows

1. **Ask → drill** — type in ⌘K → cited answer → click a citation → Entity detail → "Activity" lens → timeline.
2. **Onboard a repo** — Connectors → Add GitHub → token + repos → Sync (full) → toast "9 new, 16 edges" → Ask works.
3. **Keep memory fresh** — Extraction & Memory → Run reflection (or schedule daily) → Expertise summaries update.
4. **Wire an AI agent** — Agents & Tools → Create key → copy MCP endpoint + key into Cursor/Claude Code → agent calls `what_changed`/`who_owns` during development.
5. **Audit ownership over time** — Entity detail → as-of scrubber → "who owned this in May" + supersede trail.

---

## 8. Architecture, auth & tech

- **Stack:** Next.js (App Router) + TypeScript SPA, reusing the **existing platform `frontend/` shell + BFF** (Fastify). Add a `/bff/api/cypherxa1/*` proxy in the BFF that injects the agent JWT and forwards to cypherx-a1 `:8093` (and, for the MCP playground, to `:8094`). **No token ever reaches the browser** (BFF holds the encrypted session, exactly like the rest of the console).
- **Routing:** the SPA is served via the **edge `:8000`** (`/bff/*` proxied) — never hit `:3000` directly.
- **AuthZ in UI:** read screens need `cypherxa1:query`; mutating actions (sync/extract/consolidate/settings) need `cypherxa1:ingest`/`cypherxa1:admin`; the UI hides/disables actions the session's scopes don't allow.
- **Data fetching:** React Query; optimistic toasts for runs; SSE/poll for long syncs.
- **Visual:** dense, calm, dark-mode-first; system font; restrained color (kind-coded entity accents, green/amber/red only for status/confidence). Accessible (WCAG AA, keyboard-first, ⌘K command palette).
- **Responsive:** three-pane on desktop; the right citation rail collapses to a bottom sheet on tablet; copilot is mobile-usable.

---

## 9. Build phasing (maps to the product roadmap)

| UI phase | Screens | Depends on |
|---|---|---|
| **UI-1 (MVP)** | App shell + status, **Ask**, **Graph**, **Activity**, **Entity detail**, **Citations rail** | existing endpoints (all implemented) + BFF proxy |
| **UI-2** | **Connectors**, **Extraction & Memory**, **Knowledge bases**, **Agents & Tools (MCP)**, **Observability** | implemented endpoints + Auth key mgmt |
| **UI-3** | **Settings** (full) | the additive `settings_overrides` store (or platform control-plane) |
| **UI-4** | **Ownership & Expertise** bus-factor, **Access (resource_acls)** editor | Phase C backend |

> The first two UI phases need **zero new backend** — every screen maps to an endpoint that exists today. Only Settings-editing and bus-factor/ACLs need the small additive backend pieces noted above. This keeps the UI honest and non-over-engineered.
