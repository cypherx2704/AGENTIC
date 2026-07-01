# CLAUDE.md — auth-service (Shared Core/auth)

> Agent identity & access-management layer of the CypherX platform (Phase 02). Issues agent JWTs, service tokens, OAuth2 tokens & API keys; serves JWKS/OIDC; owns tenant lifecycle, /authorize, audit, quotas & webhooks. Platform root guide: [../../CLAUDE.md](../../CLAUDE.md). Owning spec: `archive/Manoj/phases/phase-02-auth.md`.

## What this is
The platform's **agent** IAM service (Phase 02 / SharedCore). It authenticates **agents, not end users** — end-user auth is px0's job. It mints RS256 agent JWTs (Contract 1), internal service tokens (Contract 12), OAuth2 `client_credentials` tokens, and API keys (Contract 18); publishes `/.well-known/jwks.json` + OIDC discovery; runs the `/v1/authorize` RBAC decision; owns tenant lifecycle (Contract 13), quotas (Contract 19), self-serve onboarding (Contract 20), webhooks (Contract 21), an append-only audit log (Contract 6), and a Kafka outbox relay. **Status: implemented** — a complete, non-stub service (~100 Kotlin source files, 7 integration test suites, 8 SQL migrations). Every other service verifies JWTs against this service's JWKS.

## Tech stack
- **Kotlin 2.0.21** on **JDK 21** (virtual threads enabled, `-Xjsr305=strict`), **Spring Boot 3.3.5**, **Gradle KTS** (Spring Cloud BOM 2023.0.3).
- Web / Security / Validation / Actuator (Micrometer + Prometheus).
- **Persistence:** plain Spring **JDBC** (NOT JPA) — explicit transactions are required for RLS `SET LOCAL` (Contract 13). PostgreSQL driver 42.7.4; Postgres is **external (Neon)**.
- **Kafka:** Spring Kafka (Contract 5 envelopes, String key+value, `acks=all`, idempotence on). **Cache:** Spring Data Redis → **Valkey** (Lettuce, 500ms timeout).
- **JOSE:** Nimbus `nimbus-jose-jwt:9.40` (RS256 only). **Crypto:** Argon2id (`argon2-jvm:2.11`) for client secrets; AWS KMS SDK (`kms:2.28.16`) + a local AES encryptor for signing-key envelope encryption.
- JSON logs via `logstash-logback-encoder:8.0` (Contract 6); springdoc OpenAPI `2.6.0`.
- **Tests:** JUnit5, MockK/springmockk, Testcontainers (postgres/kafka) — Docker daemon required for the RLS/integration suites.

## Repository layout
| Path | Holds |
|------|-------|
| `build.gradle.kts`, `settings.gradle.kts` | Build; `bootJar` → `auth-service.jar`. Root project `auth-service`, group `ai.cypherx`. |
| `Dockerfile` | Multi-stage temurin 21 (jdk build → jre runtime), non-root uid 10001, HEALTHCHECK `curl /readyz` (start-period 90s for Neon cold-start). |
| `openapi.yaml` | Hand-authored OpenAPI for a SUBSET of routes (revoke, audit-log, livez/readyz, jwks). Not the full surface. |
| `src/main/kotlin/ai/cypherx/auth/api/` | ~18 REST controllers (agents, api-keys, authorize, oauth, service-tokens/clients, tenants, quotas, usage, webhooks, audit + audit-export, signing-keys, onboarding, bootstrap, revocation). |
| `.../wellknown/WellKnownController.kt` | `/.well-known/jwks.json`, `/openid-configuration`, `/jwks-signed.json` (first-cycle 503). |
| `.../signing/` | `JwtMintService` (the ONLY token mint + canonical verifier), `SigningKeyService` (key gen/rotate/retire), `JwksService`, bootstrap & retirement jobs. |
| `.../service/` | Business logic: TokenMint, ServiceToken, OAuth, Authorize, Tenant, Quota, Revocation, Audit (+ chain verify/export/mirror), Onboarding, Webhook worker; `s3/`, `email/`, `captcha/` pluggable providers. |
| `.../crypto/` | `KeyEncryptor` iface + `LocalAesKeyEncryptor` / `KmsKeyEncryptor` (+ `KeyEncryptorConfig`). |
| `.../db/TenantTx.kt` | RLS transaction helper (`inTenant` / `inPlatform`). `repo/` = JDBC repositories. |
| `.../kafka/` | `AuthTopics` (contract topic names), outbox writer/relay, envelope factory, publisher. |
| `.../web/` | Security filters (`AgentJwtAuthFilter`, `RateLimitFilter`, `TraceContextFilter`), `HealthController` (`/livez`,`/readyz`), Contract-2 `GlobalExceptionHandler` + `ApiException`. |
| `.../config/` | `@ConfigurationProperties` (Auth, ServiceAuth, RateLimit, Outbox, Webhook, Onboarding, AuditPipeline, Revocation) + `SecurityConfig`. |
| `src/main/resources/` | `application.yaml` (base/cloud), `application-local.yaml`, `logback-spring.xml`. |
| `db/migrations/` | 8 versioned SQL files + `schema.sql` snapshot + `atlas.hcl` + README (PostgreSQL 16, RLS, grants). |
| `src/test/kotlin/` | RLS cross-tenant, agent-token, service-token/authorize, bootstrap, outbox, secret-redaction, misc-endpoint integration tests (+ `support/`). |

## Build, test, run
```bash
# Host
./gradlew clean bootJar            # → build/libs/auth-service.jar
./gradlew test                     # Testcontainers — needs a running Docker daemon
# Docker
docker build -t cypherx/auth-service:local .
# Compose (from repo root) — builds context ../../Shared Core/auth
docker compose -f infra/compose/docker-compose.yml up auth-service
```
- **In-container port 8080; host 8080** (`8080:8080`).
- Health: `GET /livez` (process-only `{status,version,uptime_seconds}`, never touches DB), `GET /readyz` (DB `SELECT 1` + encryptor readiness → 200/503), `GET /metrics` (Prometheus), `GET /.well-known/jwks.json`. These live at the **origin root, NO `/v1` prefix**, and are permit-all. Actuator base-path is `""`; `/metrics` is the remapped prometheus endpoint (exposure = `prometheus,health`).
- **Migrations are not auto-run** — apply via `psql "$DATABASE_URL" -f db/migrations/2026*.sql` (idempotent) or `atlas migrate apply --env local`.

## Configuration & secrets
Profiles: **default** (cloud/env-driven), **local** (`SPRING_PROFILES_ACTIVE=local`, hardcodes localhost — compose runs with EMPTY profile so env DSN wins), **test** (Testcontainers). Secrets come from **Doppler** in cloud; only `.env.example`-style env in compose. JSON property naming is global **snake_case**.
- `DATABASE_URL` / `DB_USERNAME` (`auth_user`) / `DB_PASSWORD` — Neon JDBC DSN (`currentSchema=auth`); credentials separate. Hikari pool small (PgBouncer txn-pool mode).
- `KAFKA_BROKERS`, `VALKEY_URL` — Redpanda / Valkey.
- `AUTH_ISSUER_URL`, `AUTH_PLATFORM_AUDIENCE`, `DEPLOYMENT_ID`, `ENVIRONMENT` — Contract 1 iss/aud (verifiers read these, never literals). Agent TTL 3600s, service TTL 300s, clock-skew 60s.
- `AUTH_KEY_ENCRYPTOR` = `local` | `kms`; `AUTH_LOCAL_MASTER_KEY_B64` (32-byte base64, dev only) or `KMS_SIGNING_CMK_ARN` (cloud).
- `AUTH_BOOTSTRAP_TOKEN` — one-time `X-Bootstrap-Token` for `POST /v1/admin/bootstrap`. `AUTH_EMERGENCY_ROTATE_TOKEN_FILE` — gate file for emergency signing-key rotation (absent → 403).
- `cypherx.service-auth.bootstrap-secrets.<service>` — per-service secrets for `POST /v1/service-tokens` (lowercase keys = `X-Service-Name`, must match a `service_acl` caller; compose injects via `SPRING_APPLICATION_JSON`).
- **Mock/keyless toggles (local default):** `ONBOARDING_EMAIL_PROVIDER=mock`, `ONBOARDING_CAPTCHA_PROVIDER=mock`, `AUDIT_EXPORT_STORE=local` (filesystem), audit-mirror + usage-rollup Kafka consumers **OFF** (`AUDIT_MIRROR_ENABLED`/`USAGE_ROLLUP_ENABLED=false`). Many `cypherx.auth.*` knobs in `application.yaml` (outbox, webhooks, signing-key retention 48h, redaction patterns, chain-verify ON by default).

## Contracts & cross-repo dependencies
- **Implements:** Contract 1 (JWT/JWKS/OIDC, RS256), 2 (error envelope), 5 (Kafka envelopes), 6 (JSON logs + audit), 7 (health/metrics), 10 (OpenAPI), 12 (service-auth tokens), 13 (tenant/RLS), 18 (API keys), 19 (quotas), 20 (onboarding), 21 (webhooks). `contracts/` is the source of truth.
- **Called by:** every SharedCore service verifies JWTs against `/.well-known/jwks.json` and calls `POST /v1/authorize`; xagent/llms/guardrails mint service tokens via `POST /v1/service-tokens` (bootstrap secret). No Kong in compose — services verify locally.
- **Kafka produced** (`AuthTopics`): DURABLE via outbox — `cypherx.tenant.{created,suspended,resumed,plan_changed,pending_deletion,deleted}`, `cypherx.auth.token.revoked`, `cypherx.auth.policy.changed`, `cypherx.auth.config.updated`, `cypherx.auth.quota.changed`; ADVISORY direct — `cypherx.auth.agent.registered/updated` (`agent.deactivated` reserved). **Consumed** (off by default): `cypherx.llms.usage.recorded` → `tenant_usage_counters`; `cypherx.auth.audit.appended` → object-store mirror.
- **DB:** owns Postgres schema **`auth`** (~25 tables) under runtime role **`auth_user`** (no BYPASSRLS). Tenant-scoped (RLS): `agents, api_keys, audit_log, policies*, service_clients, tenant_quotas, behavior_policies, approval_requests, approval_grants` (* `policies`/`behavior_policies` also admit `tenant_id IS NULL` platform defaults). Platform-scoped (no RLS): `tenants, signing_keys, service_acl, bootstrap_state, plan_defaults, upstream_identity, upstream_service_issuers, revoked_tokens, signup_attempts, outbox, rate_limit_config, tenant_usage_counters, webhook_*, audit_export_jobs`.

## Invariants & guards (do NOT break)
- **RS256 only — HS256 forbidden.** All minting goes through `JwtMintService`; every token carries header `kid` + `typ=JWT`. Agent JWT TTL clamped to (0, 3600s]; internal service token = 300s (`aud=["*"]`, `sub=svc:<name>`). `verify()` is forward-compatible: checks sig + exp/nbf(±skew) + iss + aud only, never rejects unknown claims.
- **Signing private keys live ONLY in `auth.signing_keys`, envelope-encrypted** (AES local / KMS cloud). There is **NO `JWT_PRIVATE_KEY` env var**. Decrypted keys are in-memory only; verifiers include demoted-but-`verifying` keys so in-flight tokens survive rotation.
- **RLS is mandatory:** all tenant-scoped DB access goes through `TenantTx.inTenant()` (`SELECT set_config('app.tenant_id', ?, true)` — bind param, never string interpolation; result-returning so it is queried not `.update()`'d). Platform tables use `inPlatform()`. `auth_user` must NOT have BYPASSRLS.
- **`tenant_id`/`agent_id` come ONLY from the verified JWT**, never the request body — `/v1/authorize` rejects a body carrying them (Contract 13 anti-pattern). Token mint resolves tenant from `X-Tenant-ID`, never the body.
- **`audit_log` is append-only:** runtime role has `SELECT, INSERT` only (no UPDATE/DELETE); per-tenant `row_hash`/`prev_row_hash` hash chain (hourly chain-verify job). Tamper-evidence — do not grant write/delete.
- **Token mint effective scopes = key.scopes ∩ agent.allowed_scopes ∩ requested.** A requested scope outside the intersection → 403.
- **Fail-open where stated:** revocation check (`RevocationChecker`: Valkey down → accept), quota enforcement, JWKS-pin caching. **Fail-soft:** audit/usage object-store and Kafka consumers default off / local — a broker-less boot must succeed. Do not make these hard dependencies.
- **`AgentJwtAuthFilter` swallows parse/verify failures** (leaves context unauthenticated so permit-all routes work); ONLY a *verified-but-revoked* token short-circuits with `401 TOKEN_REVOKED` (Contract-2 envelope written directly, bypassing `GlobalExceptionHandler`). `RateLimitFilter` runs BEFORE it (caps unauthenticated floods; fail-open with in-process backstop).
- Topic names in `AuthTopics` equal the fully-qualified `event_type` (Contract 5 §1) — renaming one is a cross-service breaking change.
- Permit-all surface (`SecurityConfig`) is deliberate and exact: `/.well-known/**`, `/metrics`, `/livez`, `/readyz`, `/oauth/token`, onboarding signup/verify/resend, `/v1/admin/bootstrap`, `POST /v1/agents/*/token`, `POST /v1/service-tokens` (those body-authenticate themselves). Stateless, CSRF/CORS/basic/form-login disabled.

## Gotchas & current status
- **Postgres is external** — there is no postgres container and no auto-migration; you must apply `db/migrations` against Neon (or local PG, init script needs superuser to create `auth_user` + `pgcrypto`/`citext`) before the service is functional. Migration files renumbered after `wp03_auth_completion` — **0005 is skipped** (numbering goes 0004 then 0006).
- `application-local.yaml` hard-codes `localhost` and ships a **dev-only AES master key + bootstrap token** (clearly marked "not a real secret") plus dev service-auth secrets for xagent/llms/guardrails. Compose intentionally runs with an EMPTY profile so the env-supplied Neon DSN is used.
- **First-cycle stubs by design:** `GET /.well-known/jwks-signed.json` returns **503** (offline RSA-4096 root signer not provisioned); OIDC discovery advertises `introspection_endpoint`/`revocation_endpoint` (`/oauth/introspect`, `/oauth/revoke`) that are NOT shipped — live revocation is via `/v1/tokens/revoke`.
- `auth.upstream_identity` ships **EMPTY** → px0 (`X-Px0-User-Token`) verification is **disabled** until a row is seeded (Component 11, moved to Phase 11). Bootstrap/manual-seed agents use the reserved `SYSTEM_USER_ID` sentinel `00000000-0000-0000-0000-000000000000`.
- Behavioral-constraints engine (Component 5c) is table + shadow seed row only — no enforcement middleware yet. Approval/step-up (Component 10) is schema-only. `/upgrade` + `/close-account` deferred to Phase 11.
- `jti` single-use is rescoped to one-time credentials only — multi-use ≤1h bearer JWTs are NOT single-use; the bearer kill-switch is Component 3c revocation (`revoked_tokens` + Valkey + kid-poisoning).
- `openapi.yaml` documents only a subset of routes; the authoritative API surface is the controllers under `api/` plus `wellknown/WellKnownController`.
