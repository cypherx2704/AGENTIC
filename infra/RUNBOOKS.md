# CypherX Operational Runbooks (WP14)

Concise, real runbooks for the local/dev full-stack (`infra/compose`) and its Neon + Valkey backing services.
Each section is self-contained: **what / when / how / verify**. Cloud (K8s/Terraform) equivalents are noted where the
local procedure diverges.

---

## 1. Neon Postgres — backup & Point-in-Time Recovery (PITR)

**What.** CypherX stores ALL durable state in one Neon database (`cypherx_platform`, per-service schemas). Neon keeps a
continuous WAL history; recovery is via **branch-from-timestamp** (its PITR primitive), plus periodic logical dumps as a
portable belt-and-suspenders backup.

**When.** Before any destructive migration or `migrate` re-run against a shared DB; after a bad deploy; on suspected data
corruption / accidental `DELETE`.

**How — logical dump (portable, offline copy).**
```bash
# Full schema+data dump from the DIRECT endpoint as owner (no -pooler; session mode).
pg_dump 'postgresql://OWNER:PW@EP-DIRECT-HOST/cypherx_platform?sslmode=require' \
  --no-owner --no-privileges -Fc -f cypherx_$(date +%Y%m%d_%H%M).dump
# Restore into a fresh branch/DB:
pg_restore --no-owner --no-privileges -d 'postgresql://OWNER:PW@EP-DIRECT-HOST/cypherx_restore?sslmode=require' cypherx_*.dump
```
Schedule daily in cloud (CronJob → object storage). Keep ≥7 daily + ≥4 weekly.

**How — Neon PITR (branch from a timestamp).**
1. Neon console → project → **Branches → Create branch → from a point in time** (pick the timestamp just BEFORE the
   incident). Neon provisions a new branch with its own endpoint hostnames.
2. Point a throwaway compose `.env` (`*_DATABASE_URL`, `MIGRATE_DATABASE_URL`) at the branch endpoints and verify the
   data is intact.
3. To "restore": either repoint production DSNs at the recovered branch (promote it), or selectively copy the affected
   rows back with `pg_dump --data-only -t <schema>.<table>` from the branch into main.

> Neon's default history window governs how far back you can branch — confirm the retention on your plan and raise it for
> anything that needs a longer RPO. PITR recovers the WHOLE database to a timestamp; for single-table recovery, branch
> then copy rows (don't roll the whole DB back).

**Verify.** After restore, run the `migrate` job (idempotent) to confirm schema parity, then `curl /readyz` on each
service against the recovered DSNs.

---

## 2. Valkey — persistence & auth hardening

**What.** Valkey (the ElastiCache substitute) holds **ephemeral, reconstructable** state: BFF sessions, idempotency keys,
rate-limit counters, the JWKS/agent-config caches, and the shared **revocation kill-switch** keys. Losing it logs every
user out and resets counters — it does NOT lose durable data (that's Neon).

**Current local config** (`docker-compose.yml`): `valkey-server --save 60 1 --appendonly no`, **no AUTH token**, bound
inside the `cypherx` network only (host-published on 6379 for dev convenience).

**Persistence — when & how.**
- Local default: RDB snapshot every 60s if ≥1 key changed (`--save 60 1`). Fine for dev (a crash loses ≤60s of sessions).
- For durability (don't lose the revocation kill-switch on restart), enable the append-only log:
  ```
  command: ["valkey-server", "--save", "60", "1", "--appendonly", "yes", "--appendfsync", "everysec",
            "--requirepass", "${VALKEY_PASSWORD}"]
  ```
  AOF `everysec` bounds loss to ~1s. The `valkey-data` volume already persists the dump/AOF across `down`/`up`.

**Auth hardening — when & how (REQUIRED outside local).**
1. Set a password: `--requirepass "${VALKEY_PASSWORD}"` and update every consumer's `VALKEY_URL` to
   `redis://:${VALKEY_PASSWORD}@valkey:6379` (auth, xagent, rag, memory, tool-registry, frontend-bff).
2. Use `rediss://` (TLS) in cloud (ElastiCache in-transit encryption).
3. Disable/rename dangerous commands (`FLUSHALL`, `CONFIG`, `KEYS`) via ACL.
4. Do NOT publish 6379 to the host in shared/cloud envs — keep it network-internal.

**Verify.** `docker exec cypherx-valkey valkey-cli -a "$VALKEY_PASSWORD" ping` → `PONG`; an unauthenticated `ping`
should return `NOAUTH`. After enabling AOF, restart the container and confirm sessions survive (re-use a session cookie).

---

## 3. `.env` secret handling

**What.** `infra/compose/.env` carries every real secret (Neon passwords, `SESSION_KEK_BASE64`, bootstrap secrets,
`REDACTION_HMAC_KEY_PLATFORM`, provider keys). `.env.example` is the committed template with **placeholders only**.

**Rules.**
- `.env` is **gitignored** (`infra/compose/.gitignore` + `infra/.gitignore` `!.env.example`). NEVER commit it.
  Verify: `git -C infra check-ignore compose/.env` (should print the path) and `git status` shows no `.env`.
- All secrets flow **via env**, never hardcoded. New secrets get a placeholder in `.env.example` + a `${VAR}` reference
  in compose (follow the existing `:-default` convention; secrets get NO default so a missing value fails fast).
- `SESSION_KEK_BASE64` must decode to **exactly 32 bytes** (the BFF refuses to boot otherwise). Generate:
  `node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"`.
- Auth bootstrap secrets must MATCH on both sides (Auth's `cypherx.service-auth.bootstrap-secrets` map AND each caller's
  `SERVICE_BOOTSTRAP_SECRET_*`).
- Cloud: do NOT use `.env` — inject via Doppler / a secrets manager / sealed K8s Secrets. The same VAR names apply.

**If a secret leaks.** Rotate it immediately (sections 4–5 for signing/redaction keys; for DB passwords run the
`migrate` job with a new `*_DB_PASSWORD` and update the matching `*_DATABASE_URL`), then purge it from history
(`git filter-repo`) and invalidate any tokens minted with the old bootstrap secret.

**Verify.** `grep -RIn "BEGIN PRIVATE KEY\|password=\|secret" --include=*.yml --include=*.yaml infra/compose` returns only
`${VAR}` references, never literals.

---

## 4. Signing-key rotation rehearsal (Auth JWKS)

**What.** Auth signs platform/agent JWTs with a rotating signing key; verifiers (llms / guardrails / xagent / rag /
tool-registry) fetch the public keys from `GET /.well-known/jwks.json` and cache them. Rotation must be
**overlapping**: publish the new key, let verifiers pick it up, then retire the old `kid` — no downtime.

**When.** Scheduled (e.g. quarterly), on suspected key compromise, or as a rehearsal before a real rotation.

**How (rehearsal).**
1. Confirm current keys: `curl -fsS localhost:8080/.well-known/jwks.json | jq '.keys[].kid'` (note the active `kid`).
2. Create + promote a new signing key via the Auth admin surface
   (`Shared Core/auth/.../SigningKeyAdminController` — `POST` a new key, mark it active). New tokens now sign with the new `kid`.
3. Wait past the verifiers' JWKS cache TTL (or bounce them) and re-fetch JWKS — both `kid`s should be present.
4. Confirm a freshly-minted token verifies end-to-end (run a task through xagent / a guardrails check).
5. **Retire** the old key (Auth admin → retire/`kid` poison). Old tokens signed with it now fail verification —
   confirm that, then confirm new tokens still pass.
6. Emergency variant: poison the compromised `kid` in the shared Valkey revocation mirror
   (`REVOCATION_KEY_PREFIX`, default `cypherx:rev:`) for immediate cross-service rejection without waiting for cache TTL.

**Verify.** JWKS lists both keys during overlap, then only the new key after retirement. A token signed by the retired
`kid` → 401 at every verifier; a token signed by the new `kid` → accepted.

---

## 5. Redaction-key rotation rehearsal (Guardrails PII HMAC)

**What.** Guardrails HMACs redacted PII with `REDACTION_HMAC_KEY_PLATFORM` so the same PII redacts to a stable token
(for correlation) without storing the cleartext. Rotating it changes the HMAC output, so rotation uses a **grace window**:
the new key becomes primary while the old key stays valid for verification until retired.

**When.** Scheduled rotation, suspected key exposure, or rehearsal.

**How (rehearsal).**
1. Note the current key id in use (the guardrails redaction-key lifecycle tracks active/retiring keys; metric
   `guardrails_redaction_keys_retired_total` counts retirements).
2. Introduce the NEW key as primary (env/secret update + the guardrails key-lifecycle admin path), keeping the OLD key
   in the verifying set for the grace window. New redactions use the new key; existing tokens still resolve via the old.
3. Run a guardrails check containing known PII and confirm the redaction token shape is stable and a violation is
   recorded (`cypherx.guardrails.violation.detected`).
4. After the grace window, **retire** the old key; the retirement job advances the lifecycle and bumps the metric.

**Verify.** During the window both keys validate; after retirement, only the new key is primary and
`guardrails_redaction_keys_retired_total` incremented. No guardrails check 5xx's during the rotation (the SLO alert
`GuardrailsViolationWriteFailing` stays quiet — see `infra/compose/observability/alerts/guardrails-slo.yml`).

---

## Appendix — host port map (local)

| Service | Host | In-container | Service | Host | In-container |
|---|---|---|---|---|---|
| auth-service | 8080 | 8080 | rag | 8087 | 8080 |
| llms-gateway | 8085 | 8080 | memory | 8088 | 8080 |
| guardrails-service | 8086 | 8080 | tool-registry | 8089 | 8080 |
| xagent | 8083 | 8080 | *(8091 freed — tool-web-search removed)* | — | — |
| demo (profile) | 8090 | 8090 | frontend-bff | 8092 | 8088 |
| frontend-app (SPA) | 3000 | 3000 | **edge (entrypoint)** | **8000** | 8000 |
| redpanda (Kafka) | 9092 | 9092 | redpanda (admin) | 9644 | 9644 |
| redpanda (schema-reg) | 8081 | 8081 | redpanda (pandaproxy) | 8082 | 8082 |
| valkey | 6379 | 6379 | minio (S3 / console) | 9000 / 9001 | 9000 / 9001 |
| Grafana (obs) | 3001 | 3000 | Prometheus (obs) | 9091 | 9090 |
| Tempo (obs) | 3200 | 3200 | Loki (obs) | 3100 | 3100 |
| OTLP gRPC / HTTP (obs) | 4317 / 4318 | 4317 / 4318 | | | |
