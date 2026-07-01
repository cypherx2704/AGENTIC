# Contract 9 — API Versioning & Pagination Standard ⚡

> **Status:** ⚡ First-cycle. Normative reference for URL versioning, sunset policy,
> cursor pagination, the full idempotency implementation contract, and request/response
> size limits.

---

## Versioning

```
All routes prefixed: /v1/, /v2/
Breaking changes: increment version. Old versions kept until explicitly sunset.
Sunset notice: minimum 90 days before removal. Sunset-Date header added.
```

| Rule | Detail |
|------|--------|
| URL prefixing | Every route is prefixed with a major version: `/v1/`, `/v2/`, … |
| Breaking changes | Increment the version. **Old versions are kept until explicitly sunset** — a contract is never silently removed. |
| Sunset notice | **Minimum 90 days** before removal. |
| Sunset signalling | The `Sunset-Date` header is added (alongside the standard RFC 8594 `Sunset` / `Deprecation` response headers defined in the OpenAPI base, Contract 10). |

---

## Pagination (all list endpoints)

```json
Request:  GET /v1/resources?limit=20&cursor=<opaque-cursor>
Response:
{
  "data": [ ... ],
  "pagination": {
    "limit":       20,
    "has_more":    true,
    "next_cursor": "<opaque-cursor>",
    "total":       null
  }
}
```

> **Cursor-based pagination only. No offset pagination.** `total` is `null` unless explicitly
> requested (it is expensive to compute).

| Field | Type | Meaning |
|-------|------|---------|
| `data` | array | The page of results. |
| `pagination.limit` | integer | Page size applied to this request. |
| `pagination.has_more` | boolean | True when further pages exist. |
| `pagination.next_cursor` | string \| null | Opaque cursor for the next page; null when none. |
| `pagination.total` | integer \| null | Total matching items; **null unless explicitly requested** (expensive). |

Request parameters: `limit` (page size) and `cursor` (opaque). No `offset` parameter is
permitted.

---

## Idempotency

```
Mutation endpoints (POST, PUT, PATCH, DELETE) MUST support:
  Header: Idempotency-Key: <client-generated-uuid>
  If same key seen within 24h: return cached response, no re-execution.
  Storage: Valkey (per-service deployment), TTL 24h, keyed by:
    idem:{service}:{tenant_id}:{api_key_id or agent_id}:{route}:{idempotency_key}
```

### Idempotency implementation contract (every service must follow)

**1. Key shape.**
```
idem:{service_name}:{tenant_id}:{principal_id}:{HTTP_METHOD}:{path}:{idempotency_key}
```
where `principal_id` is:
- `api_key_id` for external callers,
- `agent_id` for agent callers,
- `svc:{service_name}` for internal callers.

Scoping by principal prevents one principal replaying another's key.

**2. Request fingerprint.** Store `SHA256(canonical_json(request_body))` alongside the cached
response. On replay:
- same key **+ same fingerprint** → return the cached response (200/4xx/5xx as recorded),
  **DO NOT re-execute**, and set header `Idempotent-Replayed: true`.
- same key **+ different fingerprint** → reject with `409 IDEMPOTENCY_KEY_CONFLICT` and a body
  listing the divergent JSON pointers (top-level diff).

**3. In-flight collision.** A second request arriving while the first is still executing MUST
receive `409 IDEMPOTENCY_REQUEST_IN_FLIGHT` with a `Retry-After` of the remaining timeout.
Implementation: `SET NX` a sentinel `lock:{key}` with a TTL equal to the route's max execution
time; release it on completion.

**4. What is cached.**
- Final HTTP status, response headers (**excluding** `Date`, `request_id`, `trace_id`,
  `Server`), full response body, response timestamp, and the response `request_id` (for
  forensic correlation back to the original processing).
- Maximum cached body: **256 KiB**. Responses larger than this MUST set
  `Idempotent-Cacheable: false`, and the implementation MUST reject the `Idempotency-Key`
  header (return `400 IDEMPOTENCY_NOT_SUPPORTED_FOR_ROUTE`) so callers do not silently lose
  replay protection.

**5. What is NOT idempotent-cached.**
- `GET` and `HEAD`.
- `POST` routes annotated `x-idempotency-not-supported: true` in OpenAPI (e.g. streaming
  endpoints, sandboxed code execution).
- The OpenAPI lint rule in Contract 10 **rejects any mutation route that omits the idempotency
  declaration** (`required` or `not-supported`).

**6. Valkey unavailability.** **Fail closed by default** — return `503 SERVICE_UNAVAILABLE`
with `Retry-After: 5`. Rationale: silently failing open means duplicate side-effects (double
charges, double sends), which is worse than a transient outage. Per-route override
`x-idempotency-fail-open: true` is allowed **only** for read-shaped writes (e.g. a `PATCH` to
set a field to a fixed value) and **MUST be reviewed by security at PR time**.

**7. TTL.** **24h default.** Routes that produce long-running async operations (e.g. RAG
ingest, model fine-tune) MAY declare `x-idempotency-ttl-seconds` **up to 7 days** in OpenAPI.

**8. Client guidance (documented in SDK).** SDKs MUST auto-generate idempotency keys as
**UUIDv4 per request** unless the caller passes one explicitly. SDKs MUST retry on `503` and
`5xx` with the **same** `Idempotency-Key` (exponential backoff with jitter).

### New error codes added to Contract 2 (idempotency)

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `IDEMPOTENCY_KEY_CONFLICT` | 409 | Same key, different request body fingerprint |
| `IDEMPOTENCY_REQUEST_IN_FLIGHT` | 409 | Original request still processing |
| `IDEMPOTENCY_NOT_SUPPORTED_FOR_ROUTE` | 400 | Route does not support idempotent replay |

### Idempotency-related OpenAPI annotations

| Annotation | Where | Effect |
|------------|-------|--------|
| `x-idempotency-not-supported: true` | mutation route | Route opts out of idempotent replay (Rule 5). |
| `x-idempotency-fail-open: true` | route | Allows fail-open on Valkey outage; read-shaped writes only; security-reviewed (Rule 6). |
| `x-idempotency-ttl-seconds` | route | Overrides the 24h TTL, up to 7 days (Rule 7). |

### Idempotency response headers

| Header | Value | When |
|--------|-------|------|
| `Idempotent-Replayed` | `true` | Cached response returned for same key + same fingerprint (Rule 2). |
| `Idempotent-Cacheable` | `false` | Response body exceeds 256 KiB and is not cacheable (Rule 4). |

---

## Request / response size limits & content type

```
Default content type:        application/json; charset=utf-8
Max JSON body:               1 MiB (enforced at Kong)
Max multipart body:          25 MiB (only for routes declaring multipart support)
Max URL length:              8 KiB
Max header size:             16 KiB
Server response timeout:     30s default; 120s for streaming routes
```

| Limit | Value | Notes |
|-------|-------|-------|
| Default content type | `application/json; charset=utf-8` | |
| Max JSON body | 1 MiB | Enforced at Kong. |
| Max multipart body | 25 MiB | Only for routes declaring multipart support. |
| Max URL length | 8 KiB | |
| Max header size | 16 KiB | |
| Server response timeout | 30s default; 120s for streaming routes | |

> Routes that need exceptions MUST declare the override in their OpenAPI spec.
