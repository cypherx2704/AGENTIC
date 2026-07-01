# CypherX AI — Contracts

> **Phase 0 deliverable.** The single source of truth for every cross-service agreement on the
> CypherX AI platform: JWT shape, error format, event envelopes, log format, health endpoints,
> tenant model, migrations, API keys, usage metering, onboarding, webhooks.
>
> **Status:** ⚡ First Cycle — nothing else may be built until these are merged.

A *contract* is a versioned, immutable agreement. Services are built to honour contracts, **not the
other way around**. A contract is never deleted — it is deprecated. Breaking changes require a
version bump (`v1` → `v2`). See [`CHANGELOG_POLICY.md`](./CHANGELOG_POLICY.md).

This repo is **language-neutral**. It is consumed by every service repo (Kotlin, Python, TypeScript,
…) via git submodule and/or generated per-language packages (`cypherx-platform-contracts` Maven lib,
`@cypherx-ai/contracts` npm, `cypherx-contracts` PyPI — Phase 14).

---

## Layout

| Path | Contract(s) | Form |
|------|-------------|------|
| [`jwt/`](./jwt/) | 1 — JWT claims; OIDC discovery | JSON Schema + md |
| [`api/`](./api/) | 2 — Error format · 9 — Pagination · 10 — OpenAPI base | JSON Schema + OpenAPI |
| [`a2a/`](./a2a/) | 3 — A2A task request/response, delegation, task-type registry | JSON Schema + md |
| [`mcp/`](./mcp/) | 4 — MCP tool manifest | JSON Schema |
| [`kafka/`](./kafka/) | 5 — Event envelope, topic registry, first-cycle payloads | JSON Schema + md |
| [`logging/`](./logging/) | 6 — Structured log format | JSON Schema |
| [`health/`](./health/) | 7 — Health / ready / metrics endpoints | md |
| [`tracing/`](./tracing/) | 8 — Trace propagation headers | md |
| [`versioning/`](./versioning/) | 9 — API versioning, pagination, idempotency | md |
| [`service-auth/`](./service-auth/) | 12 — Service-to-service token | JSON Schema |
| [`tenant/`](./tenant/) | 13 — Tenant model, RLS pattern, well-known UUIDs | md |
| [`migrations/`](./migrations/) | 14 — Atlas migration standard + service template | md + reference layout |
| [`smoke-tests/`](./smoke-tests/) | 15 — First-cycle smoke test + Postman collection | md + JSON |
| [`approval/`](./approval/) | 16 — Step-up approval token | JSON Schema |
| [`behavior/`](./behavior/) | 17 — Behavioral policy | JSON Schema (YAML) |
| [`api-keys/`](./api-keys/) | 18 — API key format + resource ACL | md + JSON Schema |
| [`usage/`](./usage/) | 19 — Usage metering event + tenant quotas | JSON Schema |
| [`onboarding/`](./onboarding/) | 20 — External onboarding flow | md |
| [`webhooks/`](./webhooks/) | 21 — Outbound webhook delivery | md |
| [`skills/`](./skills/) | 11 — Skill definition (📋 aligned, enforced Phase 8) | JSON Schema (YAML) |

**First-cycle vs full-enterprise.** Contracts 1–10, 12–15, 18–21 are ⚡ first-cycle. Contracts 11
(Skills), 16 (Approval), 17 (Behavior) are 📋 — defined now for alignment, **enforcement turns on in
their owning phase**. Each 📋 schema carries `x-enforcement-phase` to make this explicit.

---

## Conventions

- **JSON Schema dialect:** draft 2020-12 (`https://json-schema.org/draft/2020-12/schema`). Identical
  to MCP tool schemas so tool output can flow into skill input without translation.
- **`$id` base URI:** `https://contracts.cypherx.ai/v1/<path>`. Cross-references use relative `$ref`.
- **Deployment-neutral values:** `iss` / `aud` and topic prefixes are configurable, never hardcoded
  (Contract 1 R2.2). Schemas describe shape, deployments supply values.
- **Timestamps:** RFC 3339 UTC, millisecond precision (`2026-05-22T10:00:00.000Z`).
- **Examples:** an `examples/*.json` bundle next to a schema, shaped
  `{ "$schemaRef": "../x.schema.json", "valid": [...], "invalid": [...] }`, is executed by the
  validator — `valid` docs must pass, `invalid` docs must fail.

## Validate locally

```bash
npm install
npm run check       # dependency-free structural + manifest check (works offline)
npm run validate    # full Ajv (draft 2020-12) compile + example assertions
npm run lint:openapi
npm test            # check + validate
```

CI runs all three on every PR that touches this repo (see `.github/workflows/contracts-ci.yml`).
