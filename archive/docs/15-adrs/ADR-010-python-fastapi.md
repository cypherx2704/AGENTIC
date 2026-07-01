# ADR-010 · Python 3.12 + FastAPI + uv for Non-Auth Services; Kotlin + Spring Boot for Auth

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX needs a consistent technology stack for its Python-based services (llms-gateway, guardrails, rag, memory, xAgent/ax-1, tool-registry, tool-web-search, cypherx-a1) and its authentication service. The Python services have a common profile: they make async outbound HTTP calls (to each other, to LLM providers, to Postgres via connection pool), consume and produce Kafka events, run structured logging, and expose OpenAPI-documented REST endpoints. The auth service has a distinct profile: it performs cryptographic operations (RSA signing, Argon2id hashing, AES-GCM key wrapping), integrates with a mature JVM security library ecosystem, and implements OAuth2/OIDC patterns where battle-tested JVM libraries (Nimbus JOSE+JWT, Spring Security) reduce implementation risk. These two profiles warrant different technology choices.

## Decision

**Python 3.12 + FastAPI + uv** is the standard stack for all non-auth services. Async I/O is mandatory: database access uses `psycopg3` (async driver), outbound HTTP uses `httpx` (async), Kafka uses `aiokafka`. Dependency management and virtual environments use `uv` (Astral). Structured JSON logging uses `structlog`. All services expose FastAPI's auto-generated OpenAPI docs at `/docs` (disabled in production) and health endpoints at `/livez` and `/readyz` (Contract 7). The base Docker image is `python:3.12-slim`; dependencies are pinned in `pyproject.toml` with `uv.lock` checked in.

**Kotlin 2 + Spring Boot 3.3 + JDK 21 + Gradle KTS** is the standard stack for `auth-service`. JWT operations use **Nimbus JOSE+JWT** (the reference JVM JWT library). Password hashing uses **Spring Security Crypto** (Argon2id). Key wrapping uses **AWS KMS** (cloud) or AES-256-GCM (local). Postgres access uses JDBC with HikariCP. Build system is Gradle with Kotlin DSL.

## Rationale

### Why Python + FastAPI for Most Services

FastAPI's combination of Python type annotations + Pydantic models + automatic OpenAPI generation is uniquely productive for services that are primarily I/O bound (proxying LLM calls, querying Postgres, publishing Kafka events). The framework generates accurate OpenAPI docs as a byproduct of the model definitions, keeping documentation in sync with the actual schema without extra tooling. FastAPI's native async support (`async def` endpoints, `await` throughout) means the event loop is never blocked waiting for I/O, which is the dominant operation in every non-auth service.

`uv` replaces `pip` + `virtualenv` + `pip-tools` with a single Rust-based tool that resolves and installs dependencies 10–100× faster than pip, produces a deterministic lockfile, and creates virtual environments in under a second. This matters in CI (image build time) and local dev (environment setup time).

`psycopg3` (the `asyncpg`-generation async Postgres driver) is the correct choice over `asyncpg` because it supports the `SET LOCAL` transaction parameter pattern (required for RLS per Contract 13) with idiomatic Python `async with conn.transaction():` blocks, and its cursor API maps cleanly to `aiopg` patterns that the team already knows.

### Why Kotlin + Spring Boot for Auth

`auth-service` is the only service that performs symmetric and asymmetric cryptographic operations at scale: RS256 signing of JWTs, Argon2id password hashing (deliberately slow, CPU-intensive), AES-GCM envelope encryption of private keys. The JVM ecosystem has decades of production-hardened cryptographic library development (Bouncy Castle, Nimbus JOSE) that the Python ecosystem cannot match in maturity for RS256/JWKS/OIDC patterns. Nimbus JOSE+JWT is the reference implementation for JWT in the JVM world, used by Auth0, Okta, and Spring Security itself. Using it directly in the auth service eliminates a class of subtle JWT implementation bugs (algorithm confusion, claim validation order) that have historically affected JWT libraries in other languages.

Spring Boot 3.3 with JDK 21 virtual threads (Project Loom) gives the auth service competitive concurrency without the complexity of explicit async code, at JVM-level memory safety. The Spring Security OAuth2 authorization server support handles the OAuth2 `client_credentials` flow, JWKS serving, and OIDC metadata endpoint with minimal custom code.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Go for Python services | Excellent performance and concurrency; good Postgres drivers; but FastAPI's productivity for I/O-bound services + Pydantic auto-docs is hard to match. Go would require hand-written OpenAPI schemas. Team has stronger Python expertise. |
| Node.js / TypeScript for Python services | Strong async story (event loop); good HTTP libraries; but Python dominates the AI/ML library ecosystem (sentence-transformers, LLM SDKs); switching to Node.js would require Python anyway for any ML-specific work. |
| Django REST Framework instead of FastAPI | Django is a batteries-included synchronous framework; DRF async support is partial as of 2026; FastAPI's Pydantic-native approach is more ergonomic for schema-heavy API services. |
| Poetry instead of uv | Poetry is mature but significantly slower than uv for dependency resolution; uv's lockfile format is compatible with Poetry's pyproject.toml; uv is the current best-practice choice as of 2026. |
| Python for auth service | Python JWT libraries (`python-jose`, `PyJWT`) have had historical vulnerabilities (algorithm confusion, insecure defaults); Nimbus JOSE on the JVM has a stronger security track record for the RS256 + JWKS + OIDC pattern. Argon2id in Python works but JVM Bouncy Castle's implementation is more audited for production auth workloads. |
| Go for auth service | A reasonable alternative; Go's `go-jose` library is good. Rejected because Spring Boot's OAuth2 server support provides the `client_credentials` flow, JWKS, and OIDC metadata endpoint out of the box — writing that in Go from scratch is significant effort and risk. |

## Consequences

### Positive

- FastAPI auto-generates accurate OpenAPI docs from Pydantic models — documentation is always in sync with the actual schema.
- Async all the way (psycopg3, httpx, aiokafka) means Python services have competitive concurrency for I/O-bound workloads without blocking threads.
- `uv` lock files (`uv.lock`) make dependency resolution deterministic across developer machines and CI — "works on my machine" dependency issues are eliminated.
- `structlog` produces JSON-structured logs natively (Contract 6) without a log formatter configuration step.
- Kotlin auth service gets JVM-level type safety for cryptographic operations; Nimbus JOSE + Spring Security handle the OAuth2/OIDC details correctly by construction.
- JDK 21 virtual threads in auth service mean high concurrency for token issuance without explicit async code, reducing the risk of accidental blocking on I/O.

### Negative / Trade-offs

- Two languages in the platform means two CI pipeline configurations, two Docker base images, two sets of linting/formatting tools, and two sets of team expertise to maintain.
- JVM cold-start for auth service is 3–5 seconds (vs. ~1 second for Python FastAPI with uvicorn). Mitigated by JVM warmup optimization in the Docker image (`-XX:+UseContainerSupport`) and the fact that auth is never cold in production (it has persistent traffic).
- Python's GIL means CPU-bound work (e.g., any ML inference that might be added to guardrails) is limited to one core per process. Mitigation: run multiple uvicorn workers, or offload CPU-bound work to a subprocess pool.
- `uv` is relatively new (Astral, 2023); it has less community documentation than `pip`/`poetry` for edge cases. As of 2026, it is stable and widely adopted; risk is low.
- FastAPI's Pydantic v2 models require explicit model definitions for every request/response type — more boilerplate than frameworks that use implicit dict passing, but the type safety benefit justifies the overhead.
