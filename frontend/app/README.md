# CypherX SPA (WP13)

The production single-page console for the CypherX agent platform. A Next.js (App Router)
+ TypeScript app that talks **only** to the BFF — never to the platform services directly.

## Architecture

```
Browser (this SPA)
   │  fetch(credentials: include) + X-CSRF-Token on mutations
   ▼
BFF  /bff/me  /bff/login  /bff/logout
     /bff/api/<service>/...   (auth | llms | guardrails | xagent | rag)
     /bff/api/xagent/v1/tasks/{id}/stream   (SSE)
   │  httpOnly session cookie holds the JWT; the browser never sees a token
   ▼
Platform services (auth, llms, guardrails, xagent, rag)
```

Core principles enforced in code:

- **No tokens in the browser.** Every request uses `credentials: 'include'` so the BFF's
  httpOnly session cookie carries identity. There is no token storage anywhere.
- **CSRF on mutations.** The CSRF token from `GET /bff/me` (and the readable `csrf_token`
  cookie) is echoed in the `X-CSRF-Token` header on every POST/PUT/PATCH/DELETE.
- **One error shape.** Every non-2xx response is normalized to the Contract-2 envelope
  (`{ error: { code, message, request_id, trace_id, ... } }`) and rendered consistently.
- **100% env-driven.** No service URL or secret is hardcoded — see [Environment](#environment).

## The BFF client

`src/lib/bff-client.ts` is the single network chokepoint:

- `bffFetch(path, opts)` — credentials, CSRF-on-mutation, JSON encoding, Contract-2 error
  normalization (throws a typed `BffError` carrying `status`, `code`, `traceId`, `details`).
- `api(service, path, opts)` — routes to `/bff/api/<service><path>`.
- `fetchSession()` / `login()` / `logout()` — session lifecycle; `fetchSession` caches the
  CSRF token so subsequent mutations carry it without re-reading `/bff/me`.
- `streamUrl(service, path)` — builds the absolute URL for an `EventSource` SSE stream
  (opened with `withCredentials: true` so it rides the same session cookie).

`src/lib/services.ts` provides typed, named wrappers per service area so screens never build
raw paths.

## Screens

| Route | Screen | Status |
| --- | --- | --- |
| `/login` | Tenant + admin-api-key login → `POST /bff/login` → dashboard | Full |
| `/` | Dashboard (shortcuts + session scopes) | Full |
| `/agents` | Agent list + create | Full (list depends on a BFF aggregate; open-by-id fallback) |
| `/agents/[id]` | Agent detail + **Agent Builder** (model dropdown, full `memory_scope` incl. `session`, allowed_kb_ids/allowed_tools, **two-step publish with step-2 retry**) | Full |
| `/keys` | API keys list + create with **raw-key-once modal** + revoke | Full |
| `/tasks/run` | **Task Runner** — submit (`metadata.test=true`), **422 blocked banner**, real cost/tokens, **SSE live timeline** | Full |
| `/tasks/[id]` | Task detail — ordered step/stage timeline, status/duration/tokens/cost | Full |
| `/tasks` | **Task Feed** — list with **5s long-poll** + status/agent/since filters | Full |
| `/guardrails` | Policy list + policy editor (WP07 CRUD) + violations log | Full |
| `/usage` | LLM usage/cost dashboard — `group_by`, **cache-token breakdown**, charts | Full |
| `/rag` | KB list/status + **test-query box** (WP09) | Full (shape-tolerant to the BFF's RAG proxy) |
| `/audit` | Audit-log viewer + **chain-verify button** | Full |
| `/health` | Platform health — livez/readyz of each service via the BFF | Full (tolerant to the BFF health aggregate shape) |

The Agent list, RAG admin and Platform health screens are written defensively: the exact BFF
aggregate shapes (`/bff/api/auth/v1/agents`, the RAG KB list, `/bff/health`) are owned by the
sibling BFF agent, so each screen accepts the likely shapes and degrades gracefully (clear
error + fallbacks) rather than hard-failing if the contract differs slightly.

## Design system

A small, dependency-light component library in `src/components/ui/`:
`Button`, `Input`/`Textarea`/`Select`/`Field`, `Card`/`CardHeader`/`CardBody`/`Stat`,
`Badge`/`StatusBadge`, `Table`, `Modal`, `Toast` (provider + `useToast`), `ErrorBanner`
(Contract-2 envelope display), `EmptyState`/`Loading`/`Skeleton`, `Spinner`. Plus a
dependency-free `BarChart` (SVG/divs) and a shared `TaskTimeline`. Styling is Tailwind with
CSS-variable design tokens (dark-first) in `globals.css`.

## Environment

All config is env-driven. `NEXT_PUBLIC_*` values are inlined into the browser bundle at build
time and **must contain no secrets**. Copy `.env.example` to `.env.local` for local dev.

| Key | Default | Meaning |
| --- | --- | --- |
| `NEXT_PUBLIC_BFF_URL` | _(empty)_ | BFF origin. Empty ⇒ same-origin (`/bff/...`). Set e.g. `http://localhost:8090` for split-origin dev. |
| `NEXT_PUBLIC_BFF_PREFIX` | `/bff` | Path prefix the BFF mounts under. |
| `NEXT_PUBLIC_TASK_FEED_POLL_MS` | `5000` | Task Feed long-poll interval. |
| `NEXT_PUBLIC_HEALTH_POLL_MS` | `10000` | Platform Health refresh interval. |
| `NEXT_PUBLIC_APP_NAME` | `CypherX` | Display name in the shell. |

## Develop / build / test

```bash
npm install
npm run dev      # http://localhost:3000
npm run lint     # eslint (next lint) — clean
npm run build    # next build — green
npm test         # vitest + React Testing Library — green
```

## Docker

```bash
docker build -t cypherx-spa .
docker run -p 3000:3000 -e NEXT_PUBLIC_BFF_URL= cypherx-spa
```

Multi-stage build → Next.js `standalone` output, runs as a non-root user, with a container
healthcheck against `/login`. Note `NEXT_PUBLIC_*` are baked at **build** time; pass them as
`--build-arg`/`ENV` before `npm run build` if you need a fixed BFF origin in the image.

## Tests

`vitest` + React Testing Library cover the load-bearing pieces:

- `bff-client.test.ts` — credentials, CSRF-on-mutation only, Contract-2 error mapping,
  network-failure wrapping, 204 handling, proxy URL + query building, session/login flow.
- `AgentBuilder.test.tsx` — full `memory_scope` enum, save, and the **two-step publish with
  step-2-only retry**.
- `ErrorBanner.test.tsx` — Contract-2 envelope rendering.
- `TaskTimeline.test.tsx` — ordered canonical steps + empty state.
- `utils.test.ts` — formatting helpers.
