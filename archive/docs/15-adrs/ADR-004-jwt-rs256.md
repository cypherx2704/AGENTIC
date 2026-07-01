# ADR-004 · RS256 Asymmetric JWT for Agent and Service Authentication

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX services each need to verify that incoming requests carry legitimate tokens without calling `auth-service` on every request (that would make `auth-service` a synchronous bottleneck for every API call). The authentication scheme must support multiple verifying parties (every service, Kong at the gateway, and potentially third-party integrators), key rotation without downtime, and protection against algorithm-confusion attacks. There are two main families of JWT signing: symmetric (HS256, one shared secret) and asymmetric (RS256/ES256, private key signs, public key verifies).

## Decision

All JWTs issued by CypherX — both **agent JWTs** (Contract 1) and **service-to-service tokens** (Contract 12) — use **RS256** (RSA-PKCS1v1.5 + SHA-256, 2048-bit keys). `auth-service` holds the RSA private key exclusively, stored envelope-encrypted in the `auth.signing_keys` table (AES-256-GCM, never in environment variables). The corresponding public key is served via a **JWKS endpoint** (`/.well-known/jwks.json`) that all verifying parties (services, Kong, external integrators) fetch and cache. Every token carries a `kid` (key ID) claim matching the active signing key. Key rotation is performed by adding a new key pair to the JWKS (grace period: both keys valid) and then retiring the old key after all cached tokens expire.

Service tokens (Contract 12) carry an `on_behalf_of` claim identifying the downstream agent principal; services use this to establish the tenant context without issuing a second agent JWT.

## Rationale

### Why This

Asymmetric signing means the private key is held by exactly one party (`auth-service`) and never transmitted. Any service can verify tokens using the public JWKS without ever having access to the signing material — if a service is fully compromised, the attacker gains no ability to forge tokens for other tenants. With a symmetric HS256 scheme, every service that verifies tokens must also hold the shared secret; a compromise of one service compromises the entire token ecosystem.

The JWKS endpoint pattern is the industry standard (RFC 7517, used by Auth0, Cognito, Okta, Google) and natively understood by Kong's JWT plugin and Istio's RequestAuthentication policy, enabling zero-config JWT validation at the gateway layer without custom plugin code. The `kid` claim in every token header makes key rotation non-breaking: verifiers select the correct public key by `kid`, allowing old tokens (signed with key A) and new tokens (signed with key B) to coexist during the rotation window.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| HS256 (symmetric HMAC) | The shared secret must be distributed to every verifying service. Any service compromise leaks the secret, allowing token forgery. Key rotation requires coordinated secret update across all services simultaneously (operational risk). Algorithm-confusion attacks (swapping HS256 for RS256 with the public key as the secret) are a known exploit class. |
| ES256 (ECDSA P-256) | Strictly better than RS256 in signature size and performance; however, Nimbus JOSE (the JVM library used by `auth-service`) and most Python JWT libraries have had historical edge cases with ECDSA low-s normalization. RS256 has broader battle-tested library support and simpler debugging. ES256 is a viable future upgrade. |
| Opaque tokens (random strings, validated by calling auth-service) | Eliminates the offline-verification benefit entirely. Every request requires a synchronous call to `auth-service` to validate the token — `auth-service` becomes a latency bottleneck and a single point of failure for the entire platform. |
| PASETO (Platform-Agnostic Security Tokens) | Avoids algorithm confusion by construction, but library support is immature compared to JWT, and Kong/Istio have no native PASETO support — would require custom plugins at the gateway layer. |
| Per-service shared secret (different HS256 key per service) | Reduces blast radius vs. one global HS256 secret, but still requires secret distribution and rotation coordination per service. Operationally more complex than JWKS with no meaningful security advantage over RS256. |

## Consequences

### Positive

- Services verify tokens offline (cached JWKS) with no synchronous call to `auth-service` per request; `auth-service` is not in the critical path of every API call.
- Private signing material never leaves `auth-service`; a compromised downstream service cannot forge tokens.
- JWKS endpoint is natively consumed by Kong JWT plugin and Istio `RequestAuthentication` — no custom gateway code needed.
- `kid`-based key rotation allows zero-downtime key cycling: add new key, let old tokens expire naturally, retire old key.
- Algorithm is explicit in the token header (`alg: RS256`); services configured to accept only RS256 are immune to algorithm-confusion attacks where a malicious client sends an HS256 token using the public key as the secret.
- `on_behalf_of` in service tokens (Contract 12) enables full audit trail — even machine-to-machine calls carry the originating agent identity.

### Negative / Trade-offs

- RSA key operations are slower than HMAC; RS256 signature verification is ~10–30 µs vs. ~1–2 µs for HS256. At the request rates anticipated for Phase 1–4, this is negligible. Verifiers cache the public key in memory.
- JWKS cache introduces eventual-consistency window after key rotation. Verifiers must implement a `kid`-miss → re-fetch → retry loop to avoid a hard failure during the rotation window.
- Storing private keys envelope-encrypted in Postgres (`auth.signing_keys`) means the AES key (KEK) used for the envelope must itself be managed securely. In local dev this is a throwaway DEV_AES_KEY env var; in cloud it is AWS KMS-managed — a two-tier key management dependency.
- Key material compromise in `auth-service` is catastrophic (all tokens must be immediately revoked and re-issued). This is mitigated by hardware-security module (KMS) wrapping in cloud and the principle that `auth-service` is the most hardened, most restricted service in the platform.
