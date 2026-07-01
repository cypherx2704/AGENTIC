# HTTP Header Registry (cross-service) ⚡

> **Status:** ⚡ First-cycle.
> **Authored:** 2026-06 pre-build reconciliation (WP01) — consolidated registry for the
> custom/contract headers scattered across Contracts 2, 8, 9 and 12, including the
> decided fixes on stream idempotency, X-Request-ID validation, and BFF-injected
> X-Tenant-ID.

Trace-propagation headers (`traceparent`, `tracestate`, `X-Agent-ID`) are owned by
Contract 8 — see [`../tracing/headers.md`](../tracing/headers.md). This registry covers
everything else. **Compose-parity note:** wherever "Kong" is named as the edge owner,
that is the cloud form; in the first-cycle compose runtime the first receiving service
(or the frontend BFF) performs the same duty.

---

## `Idempotency-Key`

- **Owner:** Contract 9 (`../versioning/api-versioning.md`); enforced by each mutating
  endpoint's service. First-cycle enforcement: **xAgent `POST /v1/tasks`** (Contract 15
  cases 12/13) and LLMs gateway non-stream completions.
- **Direction:** request (client → service).
- **Semantics:** client-generated UUIDv4. Same key + same body fingerprint → cached
  response replayed (no re-execution), `Idempotent-Replayed: true`. Same key + different
  fingerprint → `409 IDEMPOTENCY_KEY_CONFLICT`. In-flight duplicate →
  `409 IDEMPOTENCY_REQUEST_IN_FLIGHT` + `Retry-After`. Backing store: Valkey, 24h TTL;
  Valkey outage on an idempotent route → **fail closed `503`** (never execute without
  replay protection). **Streams (`stream=true`):** the key is *recorded* but replay-exempt
  — stream responses are never replayed from cache and the response carries
  `Idempotent-Replayed: false` (2026-06 decided fix). Error-code spelling is always
  `IDEMPOTENCY_*` (Contract 2).

## `Idempotent-Replayed`

- **Owner:** the serving service (same routes as `Idempotency-Key`).
- **Direction:** response (service → client).
- **Semantics:** `true` when the body is a cached replay for a previously completed
  request with the same key + fingerprint; `false` or absent on first execution. Always
  `false` on streaming responses (replay-exempt). Companion `Idempotent-Cacheable: false`
  marks >256 KiB responses that cannot be cached (Contract 9 Rule 4).

## `X-Request-ID`

- **Owner:** minted at the edge (Kong cloud form) or by the first receiving service /
  BFF in the compose runtime; thereafter forwarded unchanged by every service.
- **Direction:** request (edge → all downstream services); echoed as `request_id` in the
  error envelope (Contract 2), logs (Contract 6) and usage events.
- **Semantics:** MUST be a UUID. Per the 2026-06 decided fix, trace middleware
  **validates** the inbound value: non-UUID values are replaced with a synthesized UUIDv4
  and the request is logged with `request_id_generated_fallback=true` — external callers
  can no longer suppress their own violation/billing rows via an uncastable header (CI
  carries a junk-header test). Correlation only — NEVER identity, and NEVER a uniqueness
  key for billing (usage uniqueness is the gateway-minted per-call `llm_call_id`;
  `request_id` is a non-unique correlation column).

## `X-Tenant-ID`

- **Owner:** edge injection — Kong (cloud form, extracted from the validated JWT) or the
  frontend **BFF** (compose runtime, injected from the server-side Valkey session per the
  2026-06 frontend fix). Forwarded unchanged by all services.
- **Direction:** request (edge → downstream).
- **Semantics:** tenant/organisation UUID for correlation, logging and routing
  convenience. **Never trusted as identity**: every service derives the authoritative
  `tenant_id` from the verified JWT (`Authorization` or `X-Forwarded-Agent-JWT`) and MUST
  reject identity fields in request bodies (Contract 13). Optional on the Auth token-mint
  route (key-hash lookup is platform-scoped per the 2026-06 minor-batch fix).

## `X-Forwarded-Agent-JWT`

- **Owner:** Contract 12 (service-to-service auth); set by the calling service.
- **Direction:** request (service → service, internal-caller mode only).
- **Semantics:** carries the originating agent's JWT when a service calls another service
  on the agent's behalf (`Authorization` holds the caller's *service* JWT). The service
  JWT's `on_behalf_of` claim MUST equal the forwarded JWT's `agent_id`, and the receiving
  service MUST verify the forwarded JWT's signature + claims and derive
  `tenant_id`/`agent_id`/scopes from it. ABSENT in external-caller mode (bare agent or
  API-key-exchanged JWT) — both modes converge on the same downstream code path. Never
  logged; stripped before any response.

## `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (+ `X-RateLimit-Resource`)

- **Owner:** the rate-limiting service — LLMs gateway in the first cycle (Valkey
  windows); Kong plugin is the cloud form.
- **Direction:** response (on limited routes; REQUIRED on every `429`).
- **Semantics:** `Limit` = window quota, `Remaining` = calls left in the current window
  (`0` on breach), `Reset` = epoch seconds when the window resets, `Resource` =
  which limit was hit when multiple apply. Per the 2026-06 decided fix:
  `requests_per_min` is a pre-check; `tokens_per_min` is a **post-hoc window debit from
  actual usage** (no predictive token pre-check — that is 📋). Breach response is
  `429 RATE_LIMIT_EXCEEDED` with all three headers correctly typed (Contract 15 case 14).

## `Retry-After`

- **Owner:** any service returning `429 RATE_LIMIT_EXCEEDED`,
  `409 IDEMPOTENCY_REQUEST_IN_FLIGHT`, or a load-shedding/fail-closed `503`
  (e.g. Valkey outage on an idempotent route → `Retry-After: 5`).
- **Direction:** response.
- **Semantics:** integer seconds the client SHOULD wait before retrying. SDKs retry with
  backoff + jitter honouring this value, reusing the same `Idempotency-Key`.

## `X-Cypherx-Param-Clamped`

- **Owner:** LLMs gateway (provider adaptors).
- **Direction:** response.
- **Semantics:** telemetry for silent parameter adjustment — comma-separated list of
  request parameters the adaptor clamped to the provider's supported range, e.g.
  `X-Cypherx-Param-Clamped: temperature` when the unified `[0, 2]` temperature is clamped
  to Anthropic's `[0, 1]`. Rule: **no silent rounding without telemetry** — every clamp
  MUST be surfaced via this header. Absent when nothing was clamped.
