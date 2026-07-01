# tool-web-search Repository Analysis — 2026-06-11

## Overview

`tool-web-search` is a FastAPI-based MCP server that exposes a single web-search tool. The codebase is centered on a stateless request path with JWT auth, manifest publishing, provider selection, per-tenant rate limiting, idempotency replay, structured logging, metrics, and health probes.

## Root Files

- `.env.example` - Documents every supported environment variable and gives safe defaults for local development. It is the authoritative reference for service identity, auth/JWKS, Valkey, provider choice, timeouts, output cap, rate limiting, and idempotency settings.
- `.gitignore` - Keeps generated, local, and machine-specific artifacts out of version control, including virtual environments, caches, build outputs, logs, and local `.env` files.
- `.dockerignore` - Shrinks the Docker build context by excluding git metadata, caches, tests, docs, env files, and other non-runtime artifacts.
- `Dockerfile` - Builds a multi-stage container image with `uv`, installs only production dependencies in the builder stage, copies the app into a minimal runtime stage, runs as a non-root user, and exposes `/livez` for the container health check.
- `README.md` - Explains what the server does, what endpoints it exposes, how the MCP manifest and invoke flow work, how the provider adapters are selected, and how to run tests, the service, and Docker.
- `pyproject.toml` - Declares the Python package metadata, dependency groups, build backend, Ruff configuration, and pytest settings.
- `uv.lock` - Locks the resolved dependency graph so installs are reproducible across machines and CI runs.

## Package Entry Points

- `src/tool_web_search/__init__.py` - Minimal package metadata module that exposes the package version.
- `src/tool_web_search/__main__.py` - Module entry point for `python -m tool_web_search`; configures the Windows event loop policy when needed and launches Uvicorn on `HOST` and `PORT`.
- `src/tool_web_search/main.py` - Application factory. It wires logging, settings, middleware, exception handlers, routers, and the lifespan hook that creates the lazy Valkey client and warms JWKS.

## API Layer

- `src/tool_web_search/api/__init__.py` - Namespace marker for the HTTP router package.
- `src/tool_web_search/api/health.py` - Implements `/livez`, `/readyz`, and `/metrics`. Liveness is process-only, readiness reports Valkey as soft-state without gating startup, and metrics exposes Prometheus output.
- `src/tool_web_search/api/manifest.py` - Serves the MCP manifest at `/manifest`, computes a strong content-addressed ETag, and honors `If-None-Match` including wildcard and weak tags.
- `src/tool_web_search/api/invoke.py` - Handles `POST /mcp/v1/invoke`. It enforces the fine-grained scope, checks idempotency replay, applies the rate limit, validates invoke arguments, calls the provider, enforces the output cap, and stores replayable responses.

## Core Layer

- `src/tool_web_search/core/__init__.py` - Namespace marker for the shared core modules.
- `src/tool_web_search/core/config.py` - Defines the typed settings model and cached loader. It centralizes all runtime configuration, including auth, JWKS, provider settings, output limits, rate limits, idempotency, Valkey, and request body size.
- `src/tool_web_search/core/auth.py` - Verifies JWTs, supports external and internal forwarding modes, resolves the request principal, checks the coarse `tool:invoke` scope, and performs the shared verifier-side revocation mirror against Valkey in a fail-open way.
- `src/tool_web_search/core/body_limit.py` - Adds a request-body guard that rejects oversized requests before route handling, using the HTTP `Content-Length` header when available and returning the Contract-2 413 envelope.
- `src/tool_web_search/core/errors.py` - Defines the canonical API error shape and installs FastAPI exception handlers so application, validation, HTTP, and unexpected errors all render as a normalized envelope.
- `src/tool_web_search/core/logging.py` - Configures structlog and stdlib logging to emit structured JSON with service identity, environment, timestamp, level, and request correlation fields.
- `src/tool_web_search/core/metrics.py` - Declares all Prometheus counters, gauges, and histograms used by the service for invoke, manifest, Valkey health, revocation, rate limiting, and idempotency.
- `src/tool_web_search/core/trace.py` - Parses `traceparent`, request, tenant, and agent identifiers from inbound headers, binds them into contextvars, and propagates the request ID back on responses.
- `src/tool_web_search/core/valkey.py` - Implements the lazy async Redis/Valkey client wrapper. It exposes ping, get, set, set-if-absent, increment-with-expire, and close helpers while keeping failure handling with the caller.

## Service Layer

- `src/tool_web_search/services/__init__.py` - Namespace marker for domain services.
- `src/tool_web_search/services/manifest.py` - Builds the Contract-4 MCP manifest, canonicalizes it for ETag generation, and validates invoke arguments against the tool input schema with JSON-pointer-style error locations.
- `src/tool_web_search/services/rate_limit.py` - Implements the per-tenant fixed-window rate limiter backed by Valkey. It rejects over-limit requests with 429 and `Retry-After`, but fails open if Valkey is unavailable.
- `src/tool_web_search/services/idempotency.py` - Implements Valkey-backed idempotency replay for `Idempotency-Key`, including tenant scoping, replay storage, and fail-open behavior when the cache is unavailable.

## Search Provider Layer

- `src/tool_web_search/services/providers/__init__.py` - Resolves the configured provider and re-exports the provider interface and result types.
- `src/tool_web_search/services/providers/base.py` - Defines the provider protocol, the `SearchResult` data model, and a `ProviderError` used by real providers to signal upstream failures.
- `src/tool_web_search/services/providers/mock.py` - Supplies the default deterministic, network-free provider used for local development and tests. It also includes the special `__bloat__:<n>` seam used to exercise the output-cap test.
- `src/tool_web_search/services/providers/serpapi.py` - Implements the SerpApi adapter with an async HTTP call, API-key injection, error translation, and normalization of organic results into `SearchResult` objects.
- `src/tool_web_search/services/providers/brave.py` - Implements the Brave Search adapter with an async HTTP call, subscription-token auth header, error translation, and normalization of Brave web results.

## Tests

- `tests/__init__.py` - Empty package marker so pytest treats the directory as a package.
- `tests/conftest.py` - Sets deterministic test defaults, provides fake Valkey implementations, builds the ASGI test client, patches auth dependencies, and ensures the mock provider is always used in app-level tests.
- `tests/test_auth.py` - Exercises JWT verification and the verifier-side revocation mirror, including valid access, missing scope failures, token revocation cases, and fail-open behavior when Valkey is down.
- `tests/test_health.py` - Verifies liveness, readiness, and metrics behavior, including the fact that Valkey does not gate readiness.
- `tests/test_idempotency.py` - Confirms replay behavior for repeated idempotency keys, tenant-local isolation, no-key behavior, and fail-open behavior when Valkey is unavailable.
- `tests/test_invoke.py` - Covers the happy path, default limits, alias handling, fine-scope denial, schema validation with JSON pointers, and unknown-tool rejection.
- `tests/test_manifest.py` - Verifies manifest shape, required scopes, tool schema, ETag generation, `If-None-Match` behavior, wildcard handling, and cache-miss behavior.
- `tests/test_rate_limit.py` - Checks that the fixed-window limiter rejects when the counter crosses the threshold, increments below the limit, and fails open without Valkey.
- `tests/test_output_cap.py` - Forces a deterministic oversized payload through the mock provider and confirms the service returns 413 rather than streaming an oversized response.
- `tests/test_providers.py` - Verifies deterministic mock behavior, provider selection, key requirements for real providers, and request/response mapping for SerpApi and Brave via mocked HTTP.

## Functional Summary

The repo is organized around a single MCP tool, `web_search`, with a deliberate split between HTTP transport, shared core concerns, and pluggable provider adapters. The implementation is defensive and contract-driven: auth is verified before invocation, requests are correlation-traced and size-limited, the manifest is cacheable with ETags, and both rate limiting and idempotency are soft dependencies that fail open if Valkey is not reachable.

The tests are good coverage for the intended behavior. They validate the manifest contract, auth and revocation logic, idempotency replay, rate limiting, provider adapters, the output cap, and the health endpoints. The only file that is intentionally non-behavioral is `uv.lock`, which exists to make those behaviors reproducible by pinning the dependency set.