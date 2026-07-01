# Reserved Body & Metadata Key Registry ⚡

> **Status:** ⚡ First-cycle. Normative.
> **Authored:** 2026-06 pre-build reconciliation (WP01) — generalises the Contract 13
> anti-spoof rule ("identity comes from the JWT, never from a request body") into a single
> registry every service validates against. The key sets match xAgent's shipped constants
> (`xAgent/ax-1/src/agent_runtime/models/task.py` — `RESERVED_BODY_FIELDS` /
> `RESERVED_METADATA_KEYS`); code and registry MUST stay in lockstep.

Identity and correlation values flow exclusively via the **verified JWT** (identity) or via
**trace/correlation headers and server-side generation** (correlation). Caller-supplied
request bodies are *data*; they MUST NOT carry these keys — neither at the body top level
nor inside free-form `metadata` objects — because a persisted or forwarded copy would
masquerade as authoritative attribution (Contract 13 rule 3, Contract 3 "the body is data,
the JWT is authority").

---

## The reserved sets

**`RESERVED_BODY_FIELDS`** (rejected at the body top level AND in `metadata`):

```
tenant_id, trace_id, span_id, request_id, task_id, user_id, org_id
```

**`RESERVED_METADATA_KEYS`** = `RESERVED_BODY_FIELDS` ∪ `{agent_id}` (rejected as keys in
any caller-supplied `metadata` object):

```
tenant_id, trace_id, span_id, request_id, task_id, user_id, org_id, agent_id
```

> **Why `agent_id` is metadata-reserved but not body-reserved:** on xAgent
> `POST /v1/tasks`, top-level `agent_id` is a *legitimate* field — the **target** agent to
> execute (first cycle: it MUST equal `jwt.agent_id`; mismatch → `422 VALIDATION_ERROR`,
> per the 2026-06 caller-vs-target fix). Inside `metadata` it has no legitimate meaning and
> would only masquerade as attribution, so it is reserved there.

## Key-by-key semantics

| Key | Authoritative source | Why reserved |
|-----|----------------------|--------------|
| `tenant_id` | JWT `tenant_id` claim (Contract 1) | The isolation boundary. Contract 13 rule 3: resolved from the JWT, never from a body field. |
| `agent_id` (metadata only) | JWT `agent_id` claim (Contract 1) | Caller identity / attribution. Permitted at the top level of `POST /v1/tasks` ONLY as the target-agent field (validated against the JWT). |
| `user_id` | None in first cycle — end-user identity is not yet modeled (Phase 6 defines an explicit `user_ref` later) | Prevents conflating agent identity with end-user identity; the JWT-`sub` fallback was removed (2026-06 xAgent fix). |
| `org_id` | Upstream (px0/IdP) token claim, mapped to `tenant_id` by Auth | Upstream alias of the tenant boundary; accepting it from a body would bypass the Auth mapping. |
| `request_id` | `X-Request-ID` header — edge-minted (Kong, cloud form) or service/BFF-minted UUID in the compose runtime (see [`../http/headers.md`](../http/headers.md)) | Correlation only — never identity, never a billing-uniqueness key. Body copies would let callers forge or suppress correlation. |
| `trace_id` | `traceparent` W3C header (Contract 8, [`../tracing/headers.md`](../tracing/headers.md)) | Distributed-trace correlation; server-propagated, never caller-asserted via body. |
| `span_id` | `traceparent` W3C header (Contract 8) | Same as `trace_id`. |
| `task_id` | Server-generated UUID minted by xAgent at task creation (Contract 3) | Resource identifier; caller-supplied values would collide with or hijack existing tasks. |

## Enforcement — which layers validate

| Layer | Surface | Behaviour |
|-------|---------|-----------|
| **xAgent** | `POST /v1/tasks` body validation (`models/task.py`) | Top level: `extra='forbid'` — any unknown/reserved top-level key → `422 VALIDATION_ERROR`. `metadata`: keys intersected with `RESERVED_METADATA_KEYS` → `422 VALIDATION_ERROR` with `details.reason = "RESERVED_METADATA_KEY"` and the offending keys listed (sorted). |
| **LLMs gateway** | Request-body validation on `POST /v1/chat/completions` / `POST /v1/embeddings` | Caller-supplied `metadata` (and any free-form pass-through object) is checked against the same `RESERVED_METADATA_KEYS` set → `422 VALIDATION_ERROR`, same `details` shape. |

Rule for every other service: any endpoint that accepts a caller-supplied free-form
`metadata`/attributes object which the service **persists or forwards** MUST apply the same
`RESERVED_METADATA_KEYS` check with the same 422 shape. Services never *populate* these
values from bodies either — they derive them from the JWT (identity) or
headers/server-generation (correlation).

**Error shape (Contract 2):**

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "metadata must not contain reserved identity keys.",
    "details": { "reason": "RESERVED_METADATA_KEY", "keys": ["agent_id", "tenant_id"] },
    "request_id": "<uuid>",
    "trace_id": "<uuid>",
    "timestamp": "2026-06-10T00:00:00.000Z"
  }
}
```

## Change control

- Adding a key requires a PR against this registry **plus** the matching constant updates
  in every enforcing layer (xAgent `models/task.py`, LLMs body validation) and their CI
  tests, in the same change set.
- Keys are never removed — they are deprecated here with a tombstone note (Contract
  written-once rule).
- CI lint: enforcing services carry a test asserting their in-code constant equals this
  registry's set.
