# cypherx-a1 — product-development documentation

> The full design + build documentation for **cypherx-a1** (Autonomous Engineering Memory). Engineering quick-reference: [../CLAUDE.md](../CLAUDE.md). Product README: [../README.md](../README.md).

Read in order for a top-to-bottom understanding, or jump to a topic.

| # | Doc | What it covers |
|---|-----|----------------|
| 00 | [Overview & product vision](00-overview-and-product-vision.md) | Problem, base-idea critique + improvements, value prop, scope, glossary, the three layers |
| 01 | [Architecture Decision Records](01-architecture-decision-records.md) | 8 ADRs: graph store, RAG-delegated vectors, direct copilot, stateless MCP, corpus-not-in-Memory, embedding pin, bitemporal, tenancy |
| 02 | [SharedCore integration boundary](02-sharedcore-integration-boundary.md) | Per-service use + call pattern + what stays in the app; dual-header identity; fail-open/closed matrix |
| 03 | [Data model & schema](03-data-model-and-schema.md) | Every `cypherx_a1` table, the bitemporal model, RLS, indexes, FTS, migration plan |
| 04 | [RAG knowledge-base design](04-rag-kb-design.md) | KB layout, pinned embedding model, KbResolver, chunk metadata, filter constraints, citation linkage |
| 05 | [Ingestion & connector SPI](05-ingestion-and-connector-spi.md) | The connector SPI, canonical model, webhook receiver, cursors, identity resolution, GitHub connector |
| 06 | [Knowledge extraction engine](06-knowledge-extraction-engine.md) | LLM extraction prompt + schema, confidence/evidence, bitemporal supersede, idempotency + cost |
| 07 | [Hybrid retrieval & reasoning](07-hybrid-retrieval-and-reasoning.md) | Graph + RAG-dense + keyword legs, RRF fusion math, citation back-map, graph query algorithms |
| 08 | [Copilot & public API](08-copilot-and-public-api.md) | Copilot flow, the `/v1` endpoints, Citation, scopes, reserved-key guard, guardrail→HTTP mapping |
| 09 | [MCP server design](09-mcp-server-design.md) | `mcp-eng-memory` tools, Contract-4 manifest, invoke pipeline, dual-mode auth, statelessness invariant |
| 10 | [Multi-tenancy, RLS & security](10-multitenancy-rls-and-security.md) | Tenant model, FORCE RLS, the app-owned `resource_acls`, secret sealing, the cross-tenant-denial gate |
| 11 | [Eventing & outbox](11-eventing-and-outbox.md) | `cypherx.cypherxa1.*` topics, Contract-5 envelope, transactional outbox, usage metering, worker topology |
| 12 | [Observability & contracts conformance](12-observability-and-contracts-conformance.md) | Per-contract conformance scorecard, logs/metrics/tracing, health |
| 13 | [Repo placement, bootstrap & deployment](13-repo-placement-bootstrap-and-deployment.md) | CoreProjects placement, schema/role bootstrap, compose wiring, auth ACL seed, port map, local run |
| 14 | [Build plan & phasing](14-build-plan-and-phasing.md) | Dependency-ordered phases (0–3), definitions of done, smoke-test alignment |
| 15 | [Scalability & runbooks](15-scalability-and-runbooks.md) | Backfill throttling, graph cardinality, transitive-closure precompute, operational runbooks |
| 16 | [Testing strategy](16-testing-strategy.md) | Network-free pytest, the mandatory cross-tenant-denial test, connector/extraction/retrieval/MCP tests |
| 17 | [Open questions & roadmap](17-open-questions-and-roadmap.md) | Resolved decisions, open items, Phase-2+ roadmap |
