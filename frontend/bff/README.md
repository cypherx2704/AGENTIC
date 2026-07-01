# CypherX BFF (Backend-for-Frontend) — WP13

The **security boundary** between the browser SPA and the CypherX platform services.
The browser never holds a platform/agent token; it holds only an opaque, httpOnly
session-id cookie plus a readable CSRF cookie. All platform tokens, tenant context,
and credential exchange live behind this service.

Node.js + TypeScript + Fastify. 100% env-driven (no hardcoded URLs/secrets/cookies).

## Quick start

```bash
cp .env.example .env
# generate a 32-byte KEK:
node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"   # -> SESSION_KEK_BASE64
npm install
npm run build
npm test
npm run lint
npm start         # listens on BFF_HOST:BFF_PORT (default 0.0.0.0:8088)
```

Requires a reachable Valkey/Redis (`VALKEY_URL`) and an Auth service (`AUTH_URL`).

## `/bff/*` route contract

| Method | Path | Auth | CSRF | Purpose |
|---|---|---|---|---|
| `POST` | `/bff/login` | none | exempt | Platform-credential exchange → session + cookies |
| `POST` | `/bff/logout` | session | required | Destroy session, clear cookies |
| `GET`  | `/bff/me` | session | n/a (GET) | Session bootstrap (tenant, scopes, csrf token) |
| `ALL`  | `/bff/api/<service>/<path...>` | session | required on writes | Authenticated proxy to a platform service |
| `GET`  | `/bff/api/xagent/v1/tasks/{id}/stream` | session | n/a (GET) | SSE stream relay |
| `GET`  | `/livez` `/readyz` `/metrics` | none | n/a | Liveness / readiness / Prometheus |

### `POST /bff/login`
Request: `{ "tenant_id": "...", "agent_id": "...", "api_key": "<admin/platform key>" }`
(`scopes?` optional). The BFF calls Auth `POST /v1/agents/{agent_id}/token` with
`X-Tenant-ID` + body `{api_key, scopes?}`, receives the agent JWT, stores it in an
**encrypted Valkey session**, and sets the cookies.

Response `200` (the SPA's bootstrap shape — **no token**):
```json
{ "authenticated": true, "tenant_id": "...", "scopes": ["..."], "csrf_token": "..." }
```
`400` missing fields · `401` invalid credentials · `502` Auth unavailable.

### `GET /bff/me`
`200` → `{ "authenticated": true, "tenant_id", "scopes", "csrf_token" }` (and re-sets
the CSRF cookie). `401` → `{ "authenticated": false, ... }` when no session.
**px0 SSO can later populate the same session behind this identical contract.**

### `POST /bff/logout`
Destroys the server-side session and clears both cookies → `{ "authenticated": false }`.

## SPA integration contract (match these names)

- **Session cookie**: `cypherx_sid` (env `SESSION_COOKIE_NAME`) — httpOnly, Secure*, SameSite. Opaque id only; the SPA cannot read it.
- **CSRF cookie**: `cypherx_csrf` (env `CSRF_COOKIE_NAME`) — **not** httpOnly. The SPA reads it.
- **CSRF header**: `X-CSRF-Token` (env `CSRF_HEADER_NAME`). Echo the CSRF cookie value here on every `POST/PUT/DELETE/PATCH`.
- Bootstrap flow: call `GET /bff/me`; if `401`, show login → `POST /bff/login`; then echo `csrf_token` on writes.

\* `Secure` is set when `COOKIE_SECURE=true` (production/HTTPS).

## Session encryption + cookie scheme

- **Server-side sessions in Valkey**, **encrypted at rest** with **AES-256-GCM** using a
  32-byte KEK from `SESSION_KEK_BASE64`. Record format `v1.<iv>.<tag>.<ciphertext>`
  (random 96-bit IV per write; GCM auth tag → tamper-evident). A Valkey dump never
  exposes tokens or tenant ids in plaintext.
- The session holds: `tenantId`, `agentId`, `scopes`, the **downstream agent JWT**,
  its expiry, and the bound `csrfToken`. The downstream JWT **never** leaves the BFF.
- Sliding idle TTL (`SESSION_TTL_SECONDS`, default 3600), refreshed on each read.
- Cookie value is a 256-bit random base64url session id — opaque.

## CSRF mechanism (double-submit + session binding)

Enforced on every `POST/PUT/DELETE/PATCH` (GET/HEAD/OPTIONS and `/bff/login` exempt).
A request passes only when, in constant time:

```
X-CSRF-Token header === cypherx_csrf cookie === session.csrfToken
```

Binding the token to the encrypted server session defeats the classic
cookie-planting weakness of bare double-submit. Any miss → **403** and
`csrf_violations_total` is incremented (reason: `no-session` / `missing-header` / `mismatch`).

## Header injection (downstream proxy)

For `/bff/api/<service>/...` the BFF:
- **strips** hop-by-hop headers (RFC 7230) and client-supplied identity headers
  (`Authorization`, `X-Tenant-ID`, `X-Agent-ID`, `X-Forwarded-Agent-JWT`, `Cookie`, …)
  so the browser cannot spoof identity;
- **injects** `Authorization: Bearer <session downstream token>`, `X-Tenant-ID`,
  `X-Request-ID`, and a W3C `traceparent` (generated or propagated);
- routes the first path segment to the configured upstream
  (`auth`→`AUTH_URL`, `llms`→`LLMS_URL`, `guardrails`→`GUARDRAILS_URL`,
  `xagent`→`XAGENT_URL`, `rag`→`RAG_URL`);
- relays SSE for the xagent task stream without buffering;
- serves expensive dashboard GETs (`/usage`,`/cost`,`/health`,…) from a per-tenant
  ~30s cache (`DASHBOARD_CACHE_TTL_SECONDS`), keyed by tenant so no cross-tenant leak.

Upstream `Set-Cookie` and hop-by-hop response headers are stripped before reaching the
browser — the BFF owns the browser's cookies.

## Config / env keys

See [`.env.example`](./.env.example). Notable keys: `VALKEY_URL`, `SESSION_KEK_BASE64`
(32-byte base64), `SESSION_TTL_SECONDS`, `SESSION_COOKIE_NAME`, `CSRF_COOKIE_NAME`,
`CSRF_HEADER_NAME`, `COOKIE_SAMESITE`, `COOKIE_SECURE`, `CSP_POLICY`, `HSTS_MAX_AGE`,
`AUTH_URL`/`LLMS_URL`/`GUARDRAILS_URL`/`XAGENT_URL`/`RAG_URL`, `UPSTREAM_TIMEOUT_MS`,
`DASHBOARD_CACHE_TTL_SECONDS`. The process **refuses to boot** on a missing/invalid
required value (fail-fast).

## Security headers (every response)

CSP (env-tunable), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy`, `Cross-Origin-Opener/Resource-Policy`, HSTS (when `COOKIE_SECURE`),
and `Cache-Control: no-store` on `/bff/login|logout|me`.

## Tests

`npm test` (vitest) — 68 tests, no live infra (in-memory Valkey via `ioredis-mock`,
a fake fetch for upstreams). Covers session lifecycle/encryption/expiry, CSRF
valid/missing/mismatch (+metric), header injection + hop-by-hop stripping, key custody
(downstream token never in a browser response), security headers, login happy/invalid,
SSE relay, and the per-tenant cache.
