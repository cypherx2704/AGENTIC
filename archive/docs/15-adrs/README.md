# 15 · Architecture Decision Records (ADRs)

ADRs capture the reasoning behind key architecture decisions. Each ADR is immutable once accepted — if a decision is reversed, a new ADR supersedes the old one.

## ADR Index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](ADR-001-postgresql.md) | PostgreSQL as the primary database | Accepted |
| [ADR-002](ADR-002-kafka.md) | Kafka (Redpanda) for event streaming with transactional outbox | Accepted |
| [ADR-003](ADR-003-kong-istio.md) | Kong + Istio for cloud API gateway and service mesh | Accepted |
| [ADR-004](ADR-004-jwt-rs256.md) | RS256 JWT for authentication (not HS256) | Accepted |
| [ADR-005](ADR-005-multi-tenant-rls.md) | Postgres RLS for multi-tenant isolation | Accepted |
| [ADR-006](ADR-006-contract-first.md) | Contract-first design with immutable versioning | Accepted |
| [ADR-007](ADR-007-transactional-outbox.md) | Transactional outbox pattern for Kafka reliability | Accepted |
| [ADR-008](ADR-008-openai-schema.md) | OpenAI-superset schema as the LLM gateway wire format | Accepted |
| [ADR-009](ADR-009-neon-serverless.md) | Neon serverless Postgres for local + staging DB | Accepted |
| [ADR-010](ADR-010-python-fastapi.md) | Python + FastAPI for all non-auth services | Accepted |
