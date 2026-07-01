# CLAUDE.md — contracts

> Language-neutral, versioned **source of truth** for every cross-service agreement on the CypherX AI platform (JWT, errors, Kafka envelopes, logs, tenant/RLS, API keys, usage, webhooks, …). Services honour contracts — never the other way around. Platform overview: [../CLAUDE.md](../CLAUDE.md). Owning spec: `../archive/Manoj/phases/phase-00-contracts.md`.

## What this is
The Phase 0 deliverable: a `contracts/` repo of 21 numbered contracts expressed as JSON Schema (draft 2020-12), OpenAPI (YAML), and normative Markdown, plus reference SQL/Atlas templates and a Node validator. It produces **zero application code** — it is the agreement every service repo is built against. **Status: implemented.** All 42 manifest artifacts exist; both gates pass: `npm run check` → 42 artifacts / 0 problems, `npm run validate` → 25 schemas compiled / 59 example assertions / 0 errors. A few extra artifacts beyond the manifest were added in the 2026-06 pre-build reconciliation (see below).

## Tech stack
- **Specs (language-neutral):** JSON Schema **draft 2020-12** (`*.schema.json`, two as `*.schema.yaml`), OpenAPI 3.x (`api/openapi-base.yaml`), normative Markdown, plus SQL + Atlas HCL reference templates.
- **Tooling only (devDependencies):** Node 20 ESM (`"type":"module"`). `ajv` ^8 + `ajv-formats` ^3 (validation, compiled via `ajv/dist/2020.js`), `yaml` ^2 (YAML schema parsing), `@redocly/cli` ^1 (OpenAPI lint; script falls back to `spectral`).
- No Dockerfile, no runtime service, no database of its own. Consumed by service repos via git submodule and (Phase 14) generated per-language packages (`@cypherx-ai/contracts` npm, `cypherx-platform-contracts` Maven, `cypherx-contracts` PyPI).

## Repository layout
| Path | Holds |
|------|-------|
| `jwt/` | Contract 1 — JWT claims schema + OIDC discovery md + examples |
| `api/` | C2 error-format schema (with `x-known-codes`), C9 pagination, C10 `openapi-base.yaml`, `reserved-metadata-keys.md` |
| `a2a/` | C3 task-request/response + delegation schemas, `task-types.md` |
| `mcp/` | C4 MCP tool manifest schema |
| `kafka/` | C5 `event-envelope.schema.json`, `topics.md` (registry), `events/*.schema.json` (9 payloads: auth.agent.registered, llms.request.completed, guardrails.violation.detected, agent.task.completed/failed, tenant.created/suspended/plan_changed/deleted) |
| `logging/` | C6 log-format schema |
| `health/` `tracing/` `versioning/` | C7 endpoints md, C8 headers md, C9 api-versioning md |
| `service-auth/` | C12 service-token schema |
| `tenant/` | C13 `tenant-model.md` + `well-known.md` (reserved UUIDs) |
| `migrations/` | C14 `atlas-conventions.md` + `service-template/` (atlas.hcl, schema.sql, migrations/0001__init.sql, README) |
| `smoke-tests/` | C15 `first-cycle.md` + Postman collection |
| `approval/` `behavior/` `skills/` `api-keys/` `usage/` `onboarding/` `webhooks/` | C16/17/11/18/19/20/21 |
| `http/headers.md` | Consolidated cross-service HTTP header registry (extra, 2026-06) |
| `guardrails/` `billing/` | golden-suite.jsonl, jailbreak-leak-patterns.md, guardrails-rule-cost.md (extra, 2026-06; back Phase 4) |
| `scripts/` | `check-structure.mjs`, `validate.mjs` |
| `package.json` `.redocly.yaml` `CHANGELOG_POLICY.md` `README.md` | manifests + policy |

## Build, test, run
This repo is not deployed/containerised — it has no port, no `/livez`, no `infra/compose` service. It is a validated spec set:
```bash
npm install
npm run check        # offline structural + manifest check (Node built-ins only, no install needed)
npm run validate     # Ajv (draft 2020-12) compile every schema + run examples/*.json fixtures
npm run lint:openapi # redocly lint api/openapi-base.yaml --config .redocly.yaml || spectral lint
npm test             # check + validate
```
`check` is dependency-free (works with no `node_modules`); `validate`/`lint:openapi` need `npm install` first. CI: `.github/workflows/contracts-ci.yml` (GitHub Actions, canonical per stack.md; triggers on PRs touching any path + push to `main`/`feature/**`) **and** a mirror `.gitlab-ci.yml` (service repos are GitLab-hosted today) run check+validate+lint on every PR.

## Configuration & secrets
None. This repo reads **no** env vars, has no `.env.example`, no Doppler usage, no mock toggles. It *describes* deployment-neutral values that consuming services resolve at runtime — notably `iss`/`aud` (from `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE`) and Kafka topic prefixes — but never hardcodes them. Doppler credential **paths** are mandated by Contract 14 for consumers (`db/<service>/ddl_password`, `db/<service>/runtime_password`).

## Contracts & cross-repo dependencies
This **is** the contracts source; every other repo consumes it. Conventions: JSON Schema dialect draft 2020-12 (identical to MCP tool schemas so tool output flows into skill input); `$id` base `https://contracts.cypherx.ai/v1/<path>`; cross-refs are relative `$ref`; timestamps RFC 3339 UTC ms precision. Key shapes: JWT claims (C1, required `iss,sub,aud,iat,exp,jti,tenant_id,agent_id`; many `cnf`/`wkl_id`/`delegation_*`/`approval_context` reserved); error envelope (C2, required `error.{code,message,request_id,trace_id,timestamp}`, code = pattern `^[A-Z][A-Z0-9_]*$` validated, NOT a closed enum — canonical reserved list in `x-known-codes`); Kafka envelope (C5, required `event_id,event_type,schema_version,produced_at,tenant_id,producer_service,partition_key,payload`). Topic naming `cypherx.<domain>.<entity>.<event-type>`; producers/consumers pinned in `kafka/topics.md` (Auth, LLMs gateway, Guardrails, xAgent, Platform mgmt). DB: owns no schema; `migrations/service-template/` is the reference layout every service copies.

## Invariants & guards (do NOT break)
- **Written once, versioned forever.** A contract is never deleted — it is **deprecated**. Breaking change (remove/rename field, narrow type, optional→required, change meaning) = new `v2/<path>` with bumped `$id`, served alongside `v1` until sunset (min 90 days). Additive (new optional field, appended enum value, new event type, **new error code**) = non-breaking minor.
- **Forward-compatibility is mandatory.** Verifiers MUST ignore/accept unrecognised fields and MUST NOT reject documents that carry them. This is why contract objects set `additionalProperties: true` and require only validation-mandatory fields. Do not "tighten" `additionalProperties` to `false` on these objects.
- **📋 enforcement-phase rule.** Skills (11), Approval (16), Behavior (17) and reserved JWT claims (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) are defined now but enforced later; each carries `x-enforcement-phase`. Services MUST accept these shapes from day one and MUST NOT enforce them before their phase.
- **Tenant isolation (C13).** `tenant_id` is a UUID owned by Auth, resolved from the **JWT, never a request body**; every tenant-scoped table needs `tenant_id NOT NULL` + index starting with it + RLS `USING (tenant_id = current_setting('app.tenant_id')::uuid)`; `SET LOCAL` only (PgBouncer transaction mode); cross-tenant-denial CI test required for any new tenant-scoped table. Platform tenant `…0001` is read-only-by-default (writes need scope `platform:admin`); integration-test tenant `…00ff` is CI-only, rejected in prod; resolution failure must NOT default to the platform tenant.
- **Reserved keys (`api/reserved-metadata-keys.md`).** `tenant_id,trace_id,span_id,request_id,task_id,user_id,org_id` (body+metadata) and additionally `agent_id` (metadata only) MUST be rejected from caller bodies (`422 VALIDATION_ERROR`, `details.reason="RESERVED_METADATA_KEY"`). This registry MUST stay in lockstep with xAgent's `RESERVED_BODY_FIELDS`/`RESERVED_METADATA_KEYS` (`xAgent/ax-1/src/agent_runtime/models/task.py`) and LLMs gateway body validation.
- **Kafka (C5).** Every event uses the envelope; `partition_key` defaults to `tenant_id`; **compact agent topics** (`cypherx.auth.agent.registered`/`.deactivated`) MUST key by `agent_id` (both Kafka message key and `partition_key`) or all agents collapse to one record. Every **non-compact** consumer needs a paired `<topic>.dlq` (same partition count, replication 3, 30-day retention); compact topics get no DLQ. Non-`cypherx.` prefixes are forbidden except the `px0.*` allow-list, consumed by **px0-bridge only** — all other services subscribe to `cypherx.tenant.*`, never `px0.*` directly.
- **Health (C7):** `/livez` NEVER checks downstreams (liveness); `/readyz` DOES. Reserved error codes MUST NOT be reused with different meanings.
- **Process:** changes go via PR with Platform Architecture + ≥1 consuming-service-owner review; CI (`check`+`validate`+`lint:openapi`) must pass; LF line endings enforced (`.gitattributes`: `* text=auto eol=lf`).

## Gotchas & current status
- `scripts/check-structure.mjs` hard-codes the `EXPECTED` manifest (42 artifacts) — adding a new required contract file means updating that list or CI's offline gate won't catch a missing file.
- `validate.mjs` only runs example fixtures shaped `{ "$schemaRef", "valid":[…], "invalid":[…] }` next to a schema under an `examples/` dir; not every schema has fixtures (only a2a, api, api-keys, behavior, jwt, kafka(+events: only llms.request.completed), logging, mcp, service-auth, skills, usage). Cross-`$ref` resolution depends on each schema's `$id` being registered in the single shared Ajv instance.
- Two `$id`s use `.json` while the file is `.yaml` (skills, behavior) and a couple of envelope/log `$id`s drop the `.schema` infix (`event-envelope.json`, `log-format.json`) — intentional, but note when matching `$schemaRef`.
- `.gitlab-ci.yml` and the GitHub workflow are deliberately kept in sync until the CI-host choice settles (stack.md mandates GitHub Actions).
- Extra non-manifest artifacts authored in the 2026-06 pre-build reconciliation back later phases: `http/headers.md`, `api/reserved-metadata-keys.md`, `guardrails/golden-suite.jsonl`, `guardrails/jailbreak-leak-patterns.md`, `billing/guardrails-rule-cost.md`. They are checked by `validate`/`check` only for parse/keyword presence, not manifest membership.
- `validate` requires `npm install` (it imports `ajv`/`ajv-formats`/`yaml`); running it without `node_modules` fails with `ERR_MODULE_NOT_FOUND` — that is environmental, not a contract defect. `check` runs offline.