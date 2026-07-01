# CypherX Tool Registry Repository Analysis

Date: 2026-06-11

## Overview

This repository implements the CypherX Tool Registry service, a FastAPI-based control plane for registering, discovering, versioning, and health-checking MCP tools. The service is designed around tenant-aware discovery with platform shadowing, row-level security, Contract-4 manifest validation, dual-mode JWT authentication, fail-open revocation checks, and background manifest polling.

The codebase is intentionally split into a few clear layers:

- HTTP API routes in `src/tool_registry/api/`
- Request auth, error handling, tracing, logging, config, and metrics in `src/tool_registry/core/`
- PostgreSQL access and tenant/platform transaction helpers in `src/tool_registry/db/`
- Pure business logic for discovery, manifest validation, seeding, and health polling in `src/tool_registry/services/`
- End-to-end and unit tests in `tests/`
- SQL migrations and seed scripts in `db/migrations/`

## File-by-file report

### Root files

- [README.md](README.md) documents the service contract and operational behavior. It explains the registry’s purpose, the exposed endpoints, the discovery model, tenant shadowing rules, version pinning, health polling state machine, platform seed behavior, runtime configuration, and local run instructions.
- [pyproject.toml](pyproject.toml) defines the Python package metadata, runtime dependencies, development tools, packaging settings, Ruff configuration, and pytest settings. It shows that the project targets Python 3.12 and uses FastAPI, psycopg3, httpx, PyJWT, structlog, Prometheus client, and Redis.
- [Dockerfile](Dockerfile) builds a slim multi-stage container image using `uv`, installs only runtime dependencies, copies the source tree into a non-root runtime image, exposes port 8080, and adds a liveness check against `/livez`.
- [.env.example](.env.example) provides placeholder environment variables for local development. It documents service identity, PostgreSQL, Valkey, auth/JWKS, revocation settings, tool retention, health polling thresholds, platform seed URL, and discovery limits.
- [.gitignore](.gitignore) excludes local virtualenv, caches, build artifacts, environment files, and logs from version control.
- [.dockerignore](.dockerignore) keeps the Docker build context small by excluding `.git`, caches, the virtualenv, tests, docs, the root README, environment files, logs, and other non-runtime artifacts.

### `src/tool_registry/` package

- [src/tool_registry/__init__.py](src/tool_registry/__init__.py) is an empty package marker that makes `tool_registry` importable as a package.
- [src/tool_registry/__main__.py](src/tool_registry/__main__.py) provides the `python -m tool_registry` entrypoint. On Windows it switches to a selector event loop before Uvicorn starts so psycopg3 async connections work correctly; on other platforms it starts the ASGI app normally.
- [src/tool_registry/main.py](src/tool_registry/main.py) is the application factory and lifespan coordinator. It creates the FastAPI app, configures logging, installs middleware and exception handlers, mounts the health and tools routers, opens the database pool, wires the lazy Valkey client, creates a shared HTTP client for polling, seeds platform tools, warms JWKS, and runs the background manifest-health sweep until shutdown.

#### `src/tool_registry/api/`

- [src/tool_registry/api/__init__.py](src/tool_registry/api/__init__.py) is an empty package marker.
- [src/tool_registry/api/health.py](src/tool_registry/api/health.py) exposes `/livez`, `/readyz`, and `/metrics`. `livez` is process-only, `readyz` checks PostgreSQL as the hard dependency and reports Valkey as soft/unavailable without failing readiness, and `metrics` exports Prometheus data.
- [src/tool_registry/api/tools.py](src/tool_registry/api/tools.py) implements the registry’s discovery and registration endpoints. `GET /v1/tools` returns the tenant-visible union of platform and tenant tools with tenant shadowing applied, `GET /v1/tools/{name}` resolves one tool with optional version pinning, `POST /v1/tools` registers a brand-new tenant tool from a validated manifest, and `POST /v1/tools/{name}/versions` appends versions while enforcing the active-version retention cap. It also performs eager health polling after writes and increments registration metrics.

#### `src/tool_registry/core/`

- [src/tool_registry/core/__init__.py](src/tool_registry/core/__init__.py) is an empty package marker.
- [src/tool_registry/core/config.py](src/tool_registry/core/config.py) defines the pydantic settings model. It centralizes service identity, database connectivity, Valkey, JWKS and issuer settings, revocation behavior, version-retention limits, health poll thresholds, platform seed settings, and discovery limits. All values are env-overridable.
- [src/tool_registry/core/auth.py](src/tool_registry/core/auth.py) handles JWT verification and principal resolution. It supports two modes: direct agent/API-key JWTs and internal service JWTs with an `X-Forwarded-Agent-JWT`. It validates issuer, audience, expiration, enforces `on_behalf_of` matching in internal mode, extracts the tenant from the token only, and applies a fail-open Valkey-backed revocation mirror after signature verification.
- [src/tool_registry/core/errors.py](src/tool_registry/core/errors.py) defines the canonical Contract-2 API error envelope, the `ApiError` exception, and FastAPI exception handlers. It normalizes validation, HTTP, unexpected, and custom application errors into the shared `{ error: ... }` response shape with request and trace identifiers.
- [src/tool_registry/core/logging.py](src/tool_registry/core/logging.py) configures structlog and stdlib logging to emit JSON logs with the service metadata and correlation fields required by Contract 6. It injects service name, version, and environment into every log line and routes standard logging through the same JSON renderer.
- [src/tool_registry/core/metrics.py](src/tool_registry/core/metrics.py) declares the Prometheus counters and gauges used by the service. These cover Valkey availability, revocation checks, tool/version registration, version retirement, health polling outcomes, state transitions, and current tool-health status counts.
- [src/tool_registry/core/trace.py](src/tool_registry/core/trace.py) provides ASGI middleware for trace and request context propagation. It parses `traceparent`, `X-Request-ID`, `X-Tenant-ID`, and `X-Agent-ID`, stores them in context variables, binds them into structlog’s context, and echoes the request ID on HTTP responses.

#### `src/tool_registry/db/`

- [src/tool_registry/db/__init__.py](src/tool_registry/db/__init__.py) is an empty package marker.
- [src/tool_registry/db/pool.py](src/tool_registry/db/pool.py) creates the psycopg async connection pool and provides the tenant/platform transaction wrappers. `in_tenant()` sets `app.tenant_id` transaction-locally before running a query, `in_platform()` sets an empty tenant GUC for platform-wide operations, and `readyz_ping()` performs the readiness database probe.
- [src/tool_registry/db/queries.py](src/tool_registry/db/queries.py) contains the data-access layer for discovery, registration, version retrieval, capability loading, health persistence, polling inventory, and platform seed upserts. It is the module that actually executes the SQL behind the API and background jobs, always inside the appropriate tenant or platform transaction helper.
- [src/tool_registry/db/valkey.py](src/tool_registry/db/valkey.py) wraps Redis/Valkey access behind a lazy client abstraction. The rest of the app can ask it to ping, get, and close without paying the connection cost until needed, which is why readiness can report it as soft.

#### `src/tool_registry/services/`

- [src/tool_registry/services/__init__.py](src/tool_registry/services/__init__.py) is an empty package marker.
- [src/tool_registry/services/discovery.py](src/tool_registry/services/discovery.py) contains pure discovery logic. It resolves tenant-over-platform shadowing, chooses invoke URLs from `base_url` or a conventional fallback, and assembles the final response object for each tool.
- [src/tool_registry/services/manifest.py](src/tool_registry/services/manifest.py) validates Contract-4 manifests without a full JSON Schema engine. It checks the required top-level fields, name and version formats, protocol version shape, and each tool entry’s structure, and it also extracts required scopes and capability names from the manifest.
- [src/tool_registry/services/health_poll.py](src/tool_registry/services/health_poll.py) contains the pure manifest-poll state machine and the HTTP classification logic. It models `active`, `degraded`, and `offline` transitions, interprets `200` and `304` responses, treats non-2xx and transport errors as failures, and keeps the polling logic fail-soft.
- [src/tool_registry/services/health_runner.py](src/tool_registry/services/health_runner.py) orchestrates polling across the data store. It polls one tool, persists the resulting health state, sweeps all tools in a background cycle, and runs the endless lifespan loop that waits between sweeps.
- [src/tool_registry/services/seed.py](src/tool_registry/services/seed.py) builds and seeds the platform `tool-web-search` manifest from configuration. It defines the manifest structure, derives capability/scope rows, and invokes the DB seed helper so the platform tool exists at startup in an idempotent, fail-soft way.

### `tests/` suite

- [tests/__init__.py](tests/__init__.py) is an empty package marker.
- [tests/fakes.py](tests/fakes.py) supplies the fake psycopg pool and fake HTTP client used across the suite. It scripts SELECT responses, records writes, captures the tenant GUC, and lets tests simulate polling and constraint failures without live infrastructure.
- [tests/test_api_discovery.py](tests/test_api_discovery.py) verifies discovery behavior: unioning platform and tenant tools, tenant shadowing, version resolution, version pinning, missing-tool 404s, and service-unavailable handling when the DB pool is absent.
- [tests/test_api_registration.py](tests/test_api_registration.py) covers registration behavior and admin scope enforcement. It checks that non-admin principals are rejected, valid tool registration succeeds, manifest validation errors return 400, version retention retires the oldest active version, and unknown or mismatched version registrations are rejected.
- [tests/test_auth.py](tests/test_auth.py) exercises the auth dependency directly with in-memory JWTs. It covers external agent tokens, issuer/audience/expiry failures, admin-scope gating, internal service tokens with forwarded agent JWTs, and mismatch rejection for `on_behalf_of`.
- [tests/test_health_endpoints.py](tests/test_health_endpoints.py) verifies `/livez`, `/readyz`, and `/metrics`. It checks that readiness is gated on PostgreSQL, that Valkey remains soft, and that the application lifespan wires the shared HTTP client and health poll task.
- [tests/test_health_poll.py](tests/test_health_poll.py) validates manifest polling and health persistence. It covers `200`, `304`, timeout, bad JSON, and `5xx` handling, plus the state transitions across active, degraded, offline, and recovery.
- [tests/test_health_state_machine.py](tests/test_health_state_machine.py) unit-tests the pure health-state transition function. It checks success reset behavior, etag preservation across `304`, failure escalation, recovery, and configurable thresholds.
- [tests/test_manifest_validation.py](tests/test_manifest_validation.py) checks the Contract-4 manifest validator. It validates the happy path, missing required fields, naming rules, protocol-version rules, tool schema checks, tolerance for unknown fields, and scope/capability extraction.
- [tests/test_rls_cross_tenant.py](tests/test_rls_cross_tenant.py) focuses on the row-level security fix for the “marketplace hole.” It models the write-policy predicates from the migration, proves cross-tenant and forged-platform writes are rejected, verifies the platform-only path, and asserts the code always binds the tenant GUC rather than trusting caller input.
- [tests/test_seed.py](tests/test_seed.py) verifies the platform seed path. It checks that the generated web-search manifest is Contract-4 valid, that the configured base URL is respected, that the seed writes the expected DB rows, and that startup seeding is fail-soft.

### `db/migrations/`

- [db/migrations/README.md](db/migrations/README.md) explains the migration set and the row-level-security design. It documents the split read/write/platform policies, the special poller policy for `tool_health`, and the reason `NULLIF(current_setting('app.tenant_id', true), '')::uuid` is used everywhere.
- [db/migrations/20260611_0001__init.sql](db/migrations/20260611_0001__init.sql) creates the `tools` schema, the registry tables, indexes, row-level-security policies, and runtime grants. It is the foundational schema migration and explicitly fixes the cross-tenant write vulnerability by using separate `WITH CHECK` policies.
- [db/migrations/20260611_0002__seed.sql](db/migrations/20260611_0002__seed.sql) seeds the platform `tool-web-search` tool, its initial version, its capability row, and its health row. It is the reproducible SQL-only seed that mirrors the runtime platform seed.

## Functional summary

At a high level, the repo implements a tenant-aware tool registry with three main responsibilities:

1. Discover tools visible to a caller, with tenant-owned tools shadowing platform tools of the same name.
2. Register new tools and versions from validated MCP manifests while enforcing version retention.
3. Continuously track tool health by polling each tool’s manifest endpoint and updating health state in the database.

The supporting infrastructure is equally important: auth is token-based and dual-mode, errors are normalized, tracing and logs are correlated per request, metrics are exported for operational visibility, and the database layer is designed around RLS-safe tenant scoping.

## Notes

- The codebase is quite cohesive: most business logic lives in pure service modules and is covered by focused tests.
- Empty `__init__.py` files are package markers only.
- The root README and migration docs are unusually detailed and act as the canonical specification for the service.