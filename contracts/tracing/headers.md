# Contract 8 — Trace Propagation Headers ⚡

> **Status:** ⚡ First-cycle.

Every HTTP request between services MUST include **and forward** these headers. Inbound trace
context received by a service MUST be propagated unchanged onto **every outbound call** that
service makes while handling the request — this is what stitches a single distributed trace
together.

---

## Required headers (inbound → must be forwarded on all outbound calls)

| Header | Format | Description |
|--------|--------|-------------|
| `traceparent` | `00-{32-hex-trace-id}-{16-hex-span-id}-{2-hex-flags}` | W3C Trace Context. Carries the trace id (spans the whole distributed call), the current span id, and the sampling flags. |
| `tracestate` | `cypherx={tenant_id}` | W3C Trace Context vendor state. CypherX uses the `cypherx` key to carry the tenant id alongside the trace. |

### `traceparent`

W3C Trace Context format:

```
traceparent: 00-{32-hex-trace-id}-{16-hex-span-id}-{2-hex-flags}
```

- `00` — version.
- `{32-hex-trace-id}` — 32 lowercase hex chars; the trace id that spans the whole distributed
  call. The same value appears as `trace_id` in the error envelope (Contract 2) and structured
  logs (Contract 6).
- `{16-hex-span-id}` — 16 lowercase hex chars; the current span.
- `{2-hex-flags}` — 2 hex chars; sampling / trace flags.

### `tracestate`

```
tracestate: cypherx={tenant_id}
```

Vendor-specific state keyed by `cypherx`, carrying the tenant id.

---

## Custom headers (injected by Kong at edge, forwarded by all services)

These are injected by Kong at the edge (extracted from the validated JWT) and MUST be forwarded
by all services on downstream calls.

| Header | Format | Description |
|--------|--------|-------------|
| `X-Request-ID` | `{uuid}` | Unique per external request. One per inbound client call. Mirrors `request_id` in the error envelope (Contract 2). |
| `X-Tenant-ID` | `{org-uuid}` | Tenant/organisation id, **extracted from the JWT and injected by Kong**. |
| `X-Agent-ID` | `{agent-uuid}` | Agent id, **extracted from the JWT and injected by Kong**. |

> **Compose-parity note (2026-06):** Kong is the *cloud form* — it does not exist in the
> first-cycle compose runtime. Locally, services perform the edge duty themselves: each
> service's trace middleware injects `X-Request-ID` when absent and **UUID-validates** the
> inbound value, replacing a non-UUID ("junk") value with a synthesized UUIDv4 logged with
> `request_id_generated_fallback=true`, per the consolidated HTTP header registry
> ([`../http/headers.md`](../http/headers.md)). `X-Tenant-ID` is injected by the frontend
> BFF (from the server-side session) where one is in the call path; `X-Tenant-ID` /
> `X-Agent-ID` remain correlation conveniences only — every service derives the
> authoritative identity from the verified JWT, never from these headers. The Kong text
> above is unchanged and applies once the cloud edge lands.

---

## Forward-on-every-outbound-call rule

When a service receives a request, it MUST propagate the inbound trace context and the
Kong-injected identity headers onto **every outbound call** it makes while servicing that
request:

- `traceparent` (updating only its own current span id per W3C rules) and `tracestate`
  (`cypherx={tenant_id}`).
- `X-Request-ID`, `X-Tenant-ID`, `X-Agent-ID` forwarded unchanged.

Dropping any of these headers breaks distributed tracing and tenant attribution and is a
contract violation.

---

## Amendment Log (2026-06 — pre-build reconciliation)

- **Compose-parity note added (custom headers section):** no Kong exists in the first-cycle
  compose runtime — services inject/validate `X-Request-ID` themselves (UUID-validate +
  regenerate-on-junk with `request_id_generated_fallback=true`, per
  [`../http/headers.md`](../http/headers.md)); the BFF injects `X-Tenant-ID` from the
  server-side session. The Kong wording is retained as the cloud form.
