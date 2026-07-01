# CLAUDE.md — frontend

> CypherX Phase 12 (WP13) UI tier: a Next.js 15 SPA admin console (`app/`), a Node/Fastify **BFF** that is the browser↔platform security boundary (`bff/`), and a zero-dependency stdlib-Python demo agent-runner (`demo/`). The SPA talks ONLY to the BFF; the BFF holds every secret + the httpOnly session cookie and proxies to the platform services. Platform root guide: ../CLAUDE.md.

## What this is

The frontend/UI tier of the CypherX AI platform, implementing **Phase 12 / WP13** (`../archive/Manoj/phases/phase-12-frontend.md`). One repo, **three independent packages**, each with its own manifest + Dockerfile:

- **`app/`** — `cypherx-spa`, the production single-page console "CypherX Console" (Next.js 15 App Router + React 19 + TS, `output: standalone`). **Implemented** (first-cycle screens).
- **`bff/`** — `@cypherx/bff`, the Backend-for-Frontend = the trust boundary between browser and platform (Node ≥22, ESM, Fastify 4 + TS). **Implemented** (~69 test cases across 10 files).
- **`demo/`** — a stdlib-only Python prototype agent-runner BFF + single-page UI. **Implemented**; an opt-in demo harness, not the production console.

Status: **implemented** (first-cycle scope). Several SPA screens consume platform endpoints owned by other work packages that may not exist yet, and are written shape-tolerantly to degrade gracefully (see Gotchas).

## Tech stack

| Pkg | Lang / runtime | Framework | Build | Notable libs / tests |
|---|---|---|---|---|
| app | TypeScript 5.7, React 19.0.0, Next-build node | Next.js 15.5 (App Router, standalone) | `next build` | Tailwind 3.4 + CSS-var tokens; **Vitest 3** + Testing Library + jsdom |
| bff | TypeScript 5.7, Node **>=22** (ESM, `"type":"module"`) | Fastify 4.28 (`@fastify/cookie` 9) | `tsc -p tsconfig.json` → `dist/` | **ioredis** 5 (Valkey), **pino** 9; Vitest 2 + **ioredis-mock** (no live infra) |
| demo | Python **3.10+ stdlib only** | `http.server` ThreadingHTTPServer | none (2 files) | none (no pip/venv); `psql`/`docker` shelled out for sentinel reset |

## Repository layout

```
frontend/
├── app/                    # Next.js SPA (cypherx-spa)
│   ├── src/app/login/      # platform-credential login screen
│   ├── src/app/(app)/      # shell + screens: page(dashboard), agents/[agentId], keys, tasks{,/run,/[taskId]}, guardrails, usage, rag, audit, health
│   ├── src/components/     # AppShell, SessionProvider, TaskTimeline, BarChart, ui/ (Button,Card,Modal,Toast,Table,Badge,…)
│   ├── src/lib/            # bff-client.ts (ONLY network chokepoint), services.ts (typed proxy wrappers), config.ts, types.ts, useAsync.ts
│   ├── Dockerfile          # multi-stage → Next standalone, non-root, HEALTHCHECK /login
│   └── package-lock.json   # MUST include linux-musl SWC/sharp binaries for node:24-alpine build
├── bff/                    # Fastify Backend-for-Frontend (@cypherx/bff)
│   ├── src/server.ts       # prod entry: real ioredis + global fetch
│   ├── src/app.ts          # buildApp() DI factory (prod + tests share one path)
│   ├── src/config/index.ts # fail-fast env parse (KEK=32 bytes, AUTH_URL+VALKEY_URL required, …)
│   ├── src/routes/         # auth.ts (/bff/login,/logout,/me), health.ts (/livez,/readyz,/metrics), cookies.ts, sessionHook.ts
│   ├── src/proxy/          # index.ts (/bff/api/* proxy + SSE relay), headers.ts (strip/inject), cache.ts (per-tenant TTL)
│   ├── src/security/       # csrf.ts (double-submit+session-bound), headers.ts (CSP/HSTS/…), trace.ts
│   ├── src/session/        # crypto.ts (AES-256-GCM), store.ts (Valkey), types.ts
│   ├── src/upstream/authClient.ts  # the ONLY direct upstream call (POST /v1/agents/{id}/token)
│   ├── test/               # 10 *.test.ts (session, csrf, proxy, sse, login, security-headers, cache, config, crypto, health)
│   └── Dockerfile          # multi-stage → dist/, prod-deps only, non-root, HEALTHCHECK /livez
└── demo/                   # stdlib-Python demo (server.py + index.html)
    ├── server.py           # BFF: GET /api/health,/api/agent + POST /api/run; auto-provisions a demo agent
    └── Dockerfile          # python:3.12-slim-bookworm + postgresql-client (Neon sentinel reset)
```

Root `README.md` is the stock GitLab template — ignore it; the per-package READMEs are authoritative.

## Build, test, run

**app** (host): `cd frontend/app && npm install && npm run dev` → http://localhost:3000. `npm run build` (standalone) · `npm test` (vitest) · `npm run lint`.

**bff** (host): `cd frontend/bff && npm install && npm run build && npm test`. Run: `npm start` (`node dist/server.js`, listens `BFF_HOST:BFF_PORT`, default `0.0.0.0:8088`). Needs reachable Valkey + Auth. Dev: `npm run dev` (tsx watch).

**demo** (host): `python frontend/demo/server.py` → http://localhost:8090 (binds `127.0.0.1` by default). Needs auth/xagent/llms/guardrails running.

**Compose** (`../infra/compose/docker-compose.yml`):
- `frontend-bff`: in-container **8088**, host **8092**. Health `/livez /readyz /metrics`. depends_on valkey + auth/llms/guardrails/xagent (healthy).
- `frontend-app`: in-container **3000**, host **3000**. `NEXT_PUBLIC_BFF_URL` baked at **build** time (compose default `http://localhost:8092`). HEALTHCHECK `/login`.
- `edge` (Caddy, always-on Kong substitute): single entrypoint host **8000** → `/`→SPA, `/bff/*`→BFF.
- `demo`: in-container/host **8090** (predates the 8080 rule), opt-in `--profile demo`.

Note: canonical platform port is 8080, but these front-end containers deliberately do NOT use it (BFF=8088, SPA=3000, demo=8090). **Build gotcha:** `app/Dockerfile` uses `node:24-alpine` (musl); `app/package-lock.json` must carry the linux-musl optional binaries (`@next/swc-linux-{x64,arm64}-musl`, `@img/sharp-*linuxmusl*`) or `npm ci` in the deps stage breaks. The committed lockfile already includes them.

## Configuration & secrets

100% env-driven; **no hardcoded URLs/secrets/cookie names**. Only `.env.example` is committed; real secrets from **Doppler**. The BFF **refuses to boot** on any missing/invalid required value.

**bff** (`bff/.env.example`): `BFF_HOST`/`BFF_PORT` (8088), `BFF_ALLOWED_ORIGINS`, `LOG_LEVEL`; `VALKEY_URL` (**required** — server-side sessions), `SESSION_KEY_PREFIX` (`cypherx:bff:sess:`), `SESSION_TTL_SECONDS` (sliding idle, 3600); **`SESSION_KEK_BASE64`** (**required secret** — must base64-decode to exactly 32 bytes for AES-256-GCM); cookies `SESSION_COOKIE_NAME`=`cypherx_sid`, `CSRF_COOKIE_NAME`=`cypherx_csrf`, `COOKIE_SAMESITE` (lax), `COOKIE_SECURE` (false in dev), `COOKIE_PATH`/`COOKIE_DOMAIN`; `CSRF_HEADER_NAME`=`x-csrf-token`; `CSP_POLICY`, `HSTS_MAX_AGE` (31536000), `REFERRER_POLICY` (no-referrer); upstreams `AUTH_URL` (**required**), `LLMS_URL`, `GUARDRAILS_URL`, `XAGENT_URL`, `RAG_URL` (optional); `UPSTREAM_TIMEOUT_MS` (30000), `DASHBOARD_CACHE_TTL_SECONDS` (30).

**app** (`app/.env.example`): only `NEXT_PUBLIC_*`, inlined into the browser bundle at **build** time → **NO secrets**: `NEXT_PUBLIC_BFF_URL` (empty ⇒ same-origin `/bff/*`), `NEXT_PUBLIC_BFF_PREFIX` (`/bff`), `NEXT_PUBLIC_TASK_FEED_POLL_MS` (5000), `NEXT_PUBLIC_HEALTH_POLL_MS` (10000), `NEXT_PUBLIC_APP_NAME`.

**demo**: `PORT` (8090)/`BIND` (127.0.0.1, compose overrides 0.0.0.0), `AUTH_URL`/`XAGENT_URL`/`LLMS_URL`/`GUARDRAILS_URL`, `BOOTSTRAP_TOKEN`, **`DEMO_DB_URL`** (or `DATABASE_URL`; Neon ADMIN DSN — used only for the best-effort `DELETE FROM auth.bootstrap_state` sentinel reset via `psql`; falls back to `docker exec` against `PG_CONTAINER`), `DEMO_RESET_BOOTSTRAP` (default "1"), `DEMO_SYSTEM_PROMPT`, `DEMO_MODEL` (default `smart`). There is NO Postgres container — DSN targets Neon.

## Contracts & cross-repo dependencies

- **Single source of truth = `../contracts/`.** This repo honours contracts; owns **no DB schema and no Kafka topics**. BFF + SPA have **NO DB**; sessions live in Valkey; no Kafka producer/consumer here.
- **Error envelope (Contract-2):** SPA `bff-client.ts` normalizes every non-2xx to `{ error: { code, message, request_id, trace_id, details } }` and throws typed `BffError`; BFF emits the same `{ error: { code, message } }` shape (404/error handlers, login, proxy).
- **Health (Contract-7):** BFF `/livez`, `/readyz` (gated on Valkey ping), `/metrics` (Prometheus text). demo `/api/health` aggregates upstream `/readyz`.
- **Auth exchange (the only direct upstream call):** BFF→Auth `POST {AUTH_URL}/v1/agents/{agent_id}/token`, header `X-Tenant-ID`, body `{api_key, scopes?}` → agent JWT. Everything else is opaquely proxied via `/bff/api/<service>/<rest>` for `service ∈ {auth, llms, guardrails, xagent, rag}`.
- **Trace:** BFF derives/propagates `X-Request-ID` + W3C `traceparent` on request + response; relays the xagent SSE task stream (`/bff/api/xagent/v1/tasks/{id}/stream`) unbuffered.
- **Consumed platform endpoints** (`app/src/lib/services.ts`, via the proxy): Auth agents + `/keys` + `/audit-log`(+`/verify`); xAgent `/v1/tasks`(submit/get/list) + `/v1/agents/{id}/runtime` (GET/PUT); LLMs `/v1/models`,`/v1/usage`,`/v1/cost`; Guardrails `/v1/policies`(+PUT),`/v1/violations`; RAG `/v1/kbs` + `/v1/kbs/{id}/query`.
- **Called by:** the browser (SPA) and the Caddy `edge` proxy. The demo calls auth/xagent/llms/guardrails directly (its own mini-BFF), independent of `bff/`.

## Invariants & guards (do NOT break)

- **No tokens in the browser, ever.** The agent JWT lives ONLY in the encrypted Valkey session (`downstreamToken`); it never appears in `/bff/login|me|logout` bodies or any proxied response. The browser holds only the opaque httpOnly `cypherx_sid` + readable `cypherx_csrf`. SPA always uses `credentials: 'include'`; there is NO token storage.
- **BFF is the trust boundary** (no Kong in first cycle). On every proxied request it **strips** client-supplied identity/hop-by-hop headers (`authorization`, `x-tenant-id`, `x-agent-id`, `x-forwarded-agent-jwt`, `x-bootstrap-token`, `x-service-name`, `cookie`, `host`, `content-length`, connection/te/upgrade/…) and **injects** `Authorization: Bearer <session token>`, `X-Tenant-ID` (from session), `X-Request-ID`, `traceparent` last so they always win. No client header can override injected identity.
- **CSRF = double-submit + session binding.** On POST/PUT/PATCH/DELETE: `X-CSRF-Token header === cypherx_csrf cookie === session.csrfToken` (timing-safe triple match). No session on a mutating call ⇒ 403. `/bff/login` exempt (no session yet); GET/HEAD/OPTIONS exempt. Any miss → **403** + `csrf_violations_total{reason}`.
- **Session encryption at rest:** AES-256-GCM, 96-bit random IV per write, versioned record `v1.<iv>.<tag>.<ct>`. KEK must be exactly 32 bytes. Tamper/wrong-key → record dropped, treated as no session.
- **Fail-fast config:** no hardcoded URLs/secrets/cookie names; BFF refuses to boot on invalid env (KEK length, missing `AUTH_URL`/`VALKEY_URL`/`SESSION_KEK_BASE64`, `SameSite=none` without `Secure`).
- **`NEXT_PUBLIC_*` carry NO secrets** (baked into the public bundle at build).
- **Per-tenant cache isolation:** dashboard GET cache is keyed by tenant — never serve one tenant's cached body to another. Upstream `Set-Cookie` is stripped — the BFF owns the browser's cookies.
- **Security headers on every response** via a single `onSend` hook (CSP, `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy, COOP/CORP, `X-Permitted-Cross-Domain-Policies: none`, strips `X-Powered-By`, HSTS only when secure, `no-store` on `/bff/login|logout|me`).
- **BFF is JWT-exchange + session + faithful proxy only** — no tenant aggregation, no ACL transforms, no app logic ("God service" mitigation). Downstream services enforce their own RLS.
- **demo binds loopback by default** (it's unauthenticated and discloses agent config); set `BIND=0.0.0.0` only on a trusted/isolated network.

## Gotchas & current status

- **Login form is missing the Agent ID input.** `bff-client.login(tenantId, agentId, apiKey)` and `POST /bff/login` both require `agent_id` (BFF returns 400 `INVALID_REQUEST` without it), and `login/page.tsx` declares an `agentId` state var — but **no Agent ID `<Input>` is rendered**, the submit button only checks tenant+apiKey, so login currently posts an empty `agent_id` and is rejected. The Agent ID field needs to be wired back into the form before the platform-credential login works end-to-end.
- **`bff-client.ts` reads the wrong CSRF cookie name.** It hardcodes `CSRF_COOKIE = 'csrf_token'` for the cookie-fallback path, but the BFF default CSRF cookie is `cypherx_csrf`. In practice the SPA caches the token from the `/bff/me` response body (the primary path), so mutations still work after a `/bff/me`; the cookie fallback (e.g. on a cold reload before `/bff/me`) reads nothing. Align the name if relying on the cookie fallback.
- **First-cycle auth = `platform-credential`** (login: tenant_id + agent_id + admin api_key). px0 SSO (`px0-oidc`) is **deferred** and slots behind the identical `/bff/me` contract — not implemented here; there is no `BFF_AUTH_PROVIDER` switch yet.
- **Several SPA screens depend on not-yet-built endpoints** (named WP deps in the spec): Auth `GET /v1/agents` (list); LLMs `GET /v1/usage`,`/v1/models`; Guardrails `GET /v1/violations`; xAgent `GET /v1/tasks` (list) + `GET/PUT /v1/agents/{id}/runtime`. Agent list, RAG, and Health screens are written shape-tolerantly (accept `{agents|data}`, `{results|data}`, etc.) and degrade gracefully.
- **Agent test runner / `agent:mint_on_behalf` delegation, multiplexed SSE feed, workflow canvas (Phase 10), Memory/Skills/Tools dashboards, cross-tenant admin reads** — all deferred to their owning phases; not built here.
- **demo provisioning quirk:** on first use it clears the one-time Auth bootstrap sentinel (psql against `DEMO_DB_URL`, else `docker exec`), bootstraps a super-admin, creates a worker agent + key + xAgent runtime, and caches creds in `demo_credentials.json` (gitignored). Bootstrap is one-time (410 after success) — hence the reset. `run_task` re-provisions once on a stale-cache mint failure; HTTP 422 = guardrail input block (it fetches the failed timeline to surface the blocked step).
- **Dockerfiles:** app + bff use `node:24-alpine` (bff `engines: node>=22`); demo uses `python:3.12-slim-bookworm`. All three run non-root.
- Terraform/S3/CloudFront/ArgoCD bits in the phase spec are the cloud deploy-target form only — not part of this repo's compose-parity build.
