# Contract 1 — OIDC Discovery, JWKS & Key Distribution

> **Status:** ⚡ First cycle. Part of Contract 1 (JWT Claims Structure).
> **Normative reference** for how SDKs and services auto-configure trust in a CypherX Auth
> deployment. Smoke-test case 15 (Contract 15) asserts the discovery document below is reachable
> and well-formed.

`iss` and `aud` are **deployment-configurable**. Every URL below is rooted at the deployment's
`AUTH_ISSUER_URL` (e.g. `https://auth.cypherx.ai` for CypherX-managed cloud; an operator-set value
for self-hosted / white-label). Verifiers read `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` from
local config — never a hardcoded string.

---

## 1. Signing algorithm

- Algorithm: **RS256** (asymmetric). Symmetric algorithms (e.g. **HS256**) are **forbidden**.
- The JWT header `kid` (key id) MUST be present and MUST match an entry in the JWKS document.

---

## 2. OIDC discovery document — `GET {AUTH_ISSUER_URL}/.well-known/openid-configuration`

**REQUIRED.** Returns standard fields per
[RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414). SDKs and standard OIDC client libraries
auto-configure from this single URL — no manual JWKS-URL plumbing required.

### Required fields

| Field | Type | Rule |
|-------|------|------|
| `issuer` | string (URI) | MUST equal `AUTH_ISSUER_URL`. The value clients pin as the expected `iss` of every token. |
| `jwks_uri` | string (URI) | `{AUTH_ISSUER_URL}/.well-known/jwks.json`. The public JWKS endpoint (see §3). |
| `token_endpoint` | string (URI) | `{AUTH_ISSUER_URL}/oauth/token`. OAuth2 token endpoint (used by `client_credentials`, Contract 12). |
| `registration_endpoint` | string (URI) | Where external customers register a service client (`POST /v1/admin/service-clients`, Contract 12). |
| `scopes_supported` | string[] | All scopes the deployment may grant (e.g. `llm:invoke`, `memory:read`, `memory:write`, `rag:query`, `tool:invoke`, `guardrails:check`, `internal:read`, `internal:write`). |
| `response_types_supported` | string[] | OAuth2 response types. For a service/agent platform this is typically `["token"]`. |
| `grant_types_supported` | string[] | **MUST include `client_credentials`** (Contract 12 external-service path). |
| `token_endpoint_auth_methods_supported` | string[] | Auth methods accepted at `token_endpoint`, e.g. `client_secret_post`, `client_secret_basic`, `private_key_jwt` (federated OIDC / RFC 7521 `client_assertion`). |

Additional RFC 8414 fields (e.g. `jwks_signed_uri` extension, `service_documentation`) MAY be
present. Clients MUST ignore unknown fields (forward-compat).

### Smoke-test case 15 (Contract 15)

`GET {AUTH_ISSUER_URL}/.well-known/openid-configuration` returns `200` JSON containing at least:
`issuer`, `jwks_uri`, `token_endpoint`, `scopes_supported`, `grant_types_supported` (includes
`client_credentials`), and `token_endpoint_auth_methods_supported`.

---

## 3. JWKS endpoint — `GET {AUTH_ISSUER_URL}/.well-known/jwks.json`

- **Public, cacheable.** MUST be reachable **from outside the cluster** (not in-cluster-only) —
  external SDKs depend on it.
- Returns a standard JWKS document: `{ "keys": [ <JWK>, ... ] }`, each key RSA (`kty: "RSA"`,
  `use: "sig"`, `alg: "RS256"`, `kid: "<id>"`).
- A token's header `kid` MUST resolve to a key published here; otherwise the signature cannot be
  verified and the token is rejected (`KEY_REVOKED` / unknown-kid).

### Caching & refresh rules (every service MUST enforce)

- Services **MUST cache JWKS for up to 24h**.
- Services **MUST refresh on a `kid` miss** (a token whose `kid` is not in the cached set),
  **rate-limited to at most 1 refresh per minute**.

### Key rotation

- Auth rotates signing keys **every 90 days**.
- On rotation, **both the current and the previous key remain published** for the 24h cache TTL,
  so tokens minted just before rotation still verify until their (≤1h) lifetime elapses.

---

## 4. Signed JWKS bundle — `GET {AUTH_ISSUER_URL}/.well-known/jwks-signed.json`

- The same key set, **signed by an offline KMS-held RSA-4096 root** that is pinned in SDK releases.
- Used by SDK clients that **cannot rely on TLS PKI alone** (e.g. pinned-trust environments). The
  client verifies the bundle signature against the pinned root before trusting the contained keys.

---

## 5. Example discovery document

```json
{
  "issuer": "https://auth.cypherx.ai",
  "jwks_uri": "https://auth.cypherx.ai/.well-known/jwks.json",
  "jwks_signed_uri": "https://auth.cypherx.ai/.well-known/jwks-signed.json",
  "token_endpoint": "https://auth.cypherx.ai/oauth/token",
  "registration_endpoint": "https://auth.cypherx.ai/v1/admin/service-clients",
  "scopes_supported": [
    "llm:invoke",
    "memory:read",
    "memory:write",
    "rag:query",
    "tool:invoke",
    "guardrails:check",
    "internal:read",
    "internal:write"
  ],
  "response_types_supported": ["token"],
  "grant_types_supported": ["client_credentials"],
  "token_endpoint_auth_methods_supported": [
    "client_secret_post",
    "client_secret_basic",
    "private_key_jwt"
  ]
}
```

> A self-hosted/white-label deployment serves the same shape with its operator-set `issuer` and the
> corresponding `{issuer}/.well-known/...` URLs.

---

## 6. Cross-reference — `iss` vs JWKS URL per environment (Phase 1 Component 5)

> Per Phase 1 Component 5, `iss` is a **stable opaque identifier** while the JWKS URL is
> **discovered per-environment** at `https://auth.<env>.cypherx.ai/.well-known/jwks.json`;
> verifiers configure the JWKS URL per environment and MUST NOT derive it from `iss`.
