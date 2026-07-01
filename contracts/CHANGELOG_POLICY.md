# Contract Change Policy

> How to propose, version, and retire a contract. A contract is a promise to every service on the
> platform; this policy keeps that promise stable.

## Principles

1. **Written once, versioned forever.** A contract is never deleted — it is deprecated.
2. **Additive by default.** New *optional* fields, new enum values appended at the end, new event
   types, and new error codes are **non-breaking** and ship as a minor bump.
3. **Breaking changes bump the major version.** Removing/renaming a field, narrowing a type, making
   an optional field required, or changing the meaning of a value is breaking → new `v2` schema
   alongside `v1`. Both are served until every consumer migrates.
4. **Forward-compatibility is mandatory.** Verifiers MUST ignore unrecognised fields/claims and MUST
   NOT reject documents that carry them (Contract 1 forward-compatibility rule). This is what lets
   later phases add fields without breaking first-cycle services.

## Versioning

- Each schema declares `$id: https://contracts.cypherx.ai/v1/<path>` and a payload-level
  `schema_version` (SemVer) where the wire format carries one (A2A, Kafka, MCP).
- Breaking change → copy to `v2/<path>`, increment `$id`; keep `v1` until sunset.
- **Sunset:** minimum 90 days’ notice. A `Sunset` / `Deprecation` header (HTTP) or a
  `deprecated: true` schema annotation marks the retiring version.

## Process

1. Open a PR against this repo. PR description states: contract touched, breaking vs non-breaking,
   affected consumers, migration note.
2. CI must pass: `npm run check`, `npm run validate`, `npm run lint:openapi`.
3. Review required from the Platform Architecture team **and** at least one consuming service owner.
4. On merge, append an entry to the per-contract changelog and (for wire formats) bump
   `schema_version`.
5. Breaking change additionally requires a backward-compatibility CI check (new version must be
   readable by old consumers) and an entry on the public contracts site (Phase 13).

## Enforcement-phase annotation

📋 contracts (Skills 11, Approval 16, Behavior 17, and reserved JWT claims) are defined now for
alignment but **enforced later**. Each carries `x-enforcement-phase` (e.g. `"phase-08"`). Services
MUST accept these shapes from day one (forward-compat) and MUST NOT enforce them before their phase.
