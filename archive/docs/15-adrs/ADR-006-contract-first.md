# ADR-006 · Contract-First Design with Immutable Published Contracts

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX is built as a set of independently deployable polyglot microservices (Kotlin, Python) that communicate over HTTP and Kafka. Without a single source of truth for the wire format of every API, event envelope, and shared data structure, teams end up in a pattern where one service's idea of a field name or type diverges from another's — producing integration bugs that only surface at runtime. In a multi-repo codebase with services built in different languages, code sharing is not an option: the coordination mechanism must be language-agnostic and enforced at CI level, not by convention.

## Decision

The `contracts/` repository is the **single source of truth** for every cross-service agreement. Contracts 1–21 cover: agent JWT schema (1), error envelope (2), A2A task format (3), MCP tool invocation (4), Kafka event envelope (5), structured log schema (6), health-check endpoints (7), tracing propagation (8), OIDC/JWKS endpoints (9), tenant onboarding API (20), billing/metering events (21), and all other cross-service interfaces. Contracts are JSON Schema 2020-12 and/or OpenAPI 3.x documents with normative Markdown. A Node 20 ESM validator (`ajv`) runs in CI against every proposed change. **Contracts are immutable after publication**: once a contract version is merged to `main`, its schema cannot be edited. Breaking changes require a new version (`v2`) published alongside `v1`; additive optional fields can be added to an existing version without a bump. Services adapt to contracts; contracts never change to accommodate a service implementation. `contracts/amendments/plan-fixes.json` carries binding overrides to phase-doc language where the two sources conflict.

## Rationale

### Why This

Contract-first is the only design discipline that can enforce cross-team, cross-language API compatibility in a microservices platform built in parallel by multiple teams. When the contract exists and is gated by CI before any service code is written, the question "does my implementation match the API?" becomes mechanically answerable — run the validator. Without a contract, that question can only be answered by reading another team's code or running an end-to-end integration test.

Immutability after publication is a critical property: it means that any service that was certified against Contract N remains correct forever — you do not need to re-test it when another service is updated, as long as contracts are not modified. Breaking changes become visible as new versioned contracts, which triggers a deliberate migration effort rather than a silent regression.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Code-first with generated OpenAPI (e.g., FastAPI auto-schema) | The schema is a byproduct of code; it changes whenever code changes. Two services generating their own schemas with no shared source of truth will drift. The schema exists only in the server's OpenAPI output, which the client has to fetch and may be out of date. |
| Shared library / SDK | Works within one language (e.g., a shared Python package), but CypherX has both Kotlin and Python services. A shared Kotlin library cannot be used by Python services. A shared proto/Avro schema is closer to contract-first but adds a build tool dependency. |
| Informal documentation (Confluence / Notion) | Not machine-readable; cannot be validated in CI; diverges from implementation as soon as someone makes a "quick fix" without updating docs. |
| GraphQL schema as the single contract | GraphQL federation is powerful for query-centric APIs, but CypherX services expose REST + SSE endpoints, not a unified graph. Forcing GraphQL on all services (including Kafka events) would be architecturally disruptive. |
| AsyncAPI for event contracts + OpenAPI for HTTP | Valid approach, but adds two schema languages. CypherX uses JSON Schema (superset of both) with normative Markdown sections for semantics; the validator handles both HTTP body schemas and event envelope schemas in one tool. |

## Consequences

### Positive

- CI rejects any code change that produces a payload violating a published contract schema — integration bugs are caught before merge, not in production.
- New services can be built by any team in any language by reading the contract documents; no need to inspect existing service code.
- Contract immutability means version `v1` of Contract 1 is a stable reference forever — no "which version are you on?" confusion.
- Forward-compatibility mandate (`additionalProperties: true` in JSON Schema; verifiers ignore unknown fields) means the platform can add new fields to existing contracts as additive changes without coordinated service upgrades.
- Additive-only changes without a version bump are explicitly permitted (per `amendments/plan-fixes.json`), reducing the overhead of the immutability rule for small incremental enhancements.
- The `contracts/smoke-tests/` directory defines Contract-15 (15 smoke test cases) that are the official "first-cycle spine done" acceptance criteria — a mechanical pass/fail gate.

### Negative / Trade-offs

- Contract-first requires up-front design investment before any service code is written. For Phase 0, all 21 contracts were designed before Phase 1 implementation began — this is a real lead-time cost.
- Immutability means a design mistake in a published contract requires a `v2` alongside `v1`, and a migration period where both versions are supported. This adds maintenance burden proportional to the number of breaking mistakes made in v1.
- The CI gate on `contracts/` is a bottleneck: every PR to any service that touches a wire interface must also update the contract and pass the validator. Teams unused to contract-first workflows initially resist this friction.
- `amendments/plan-fixes.json` as a binding override layer means there are now two sources of truth (`contracts/` documents + `amendments/`). A reader must always check amendments before treating a contract document as gospel. This is manageable but adds a layer of indirection.
- Currently, the validator is JSON Schema / ajv-based. For Kafka event schemas, the validator checks the envelope structure but cannot enforce the payload schema of every event type without a per-event schema file. Closing this gap is a future hardening task (Phase 13).
