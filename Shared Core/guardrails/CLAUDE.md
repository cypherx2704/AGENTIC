# CLAUDE.md — guardrails-service (Shared Core/guardrails)

> Input/output safety service for the CypherX agent spine: deterministic prompt-injection / PII / toxicity checks with HMAC redaction, per-tenant policies, fail-modes and Contract-19 usage metering. A single FastAPI app (`guardrails_service`) on the platform stack. Platform root guide: [../../CLAUDE.md](../../CLAUDE.md).

## What this is

The platform's **Guardrails Service** (Phase 04 / WP02+WP03+WP07). xAgent calls it on the agent spine (`auth→xagent→guardrails→llms`): before sending a prompt to the LLM (`POST /v1/check/input`) and before returning the model's response (`POST /v1/check/output`). It evaluates the 11 built-in rules (+ tenant custom rules) under the resolved effective policy, applies deterministic `[REDACTED:...]` HMAC tokens, returns an `allow|warn|redact|block` decision, and persists violations + usage off the hot path via a transactional outbox to Kafka. Status: **implemented** (stub classifier by default; detoxify is an optional prod image). ~155 test functions, ruff + mypy-strict configured.

**IMPORTANT — this repo's old CLAUDE.md was wrong.** On `development` this is NO LONGER the divergent 4-service Qdrant/llm-router monorepo (`GR_`-prefixed env, ports 908x, `sanitize` verdict, no Kafka). It is now a single platform-integrated FastAPI service: root `Dockerfile` (`EXPOSE 8080`, `python -m guardrails_service`, `/livez`), Neon-backed (`schema=guardrails`, role=`grd_user`), `CLASSIFIER_MODE=stub`, full `contracts/` honouring. Trust the code in `src/guardrails_service/`.

## Tech stack

- **Python 3.12**, **uv** (`uv.lock` committed), **hatchling** build (`packages = ["src/guardrails_service"]`).
- **FastAPI + uvicorn[standard]**, **Pydantic v2 / pydantic-settings**.
- **psycopg[binary,pool] 3** (async, Postgres/Neon) — NOT asyncpg; **aiokafka** (outbox→Redpanda); **redis 5** (Valkey client); **pyjwt[crypto]** (RS256 JWKS); **structlog**; **prometheus-client**.
- Optional `ml` extra: **detoxify** (→ torch) — PROD only; default stub avoids it.
- Dev group: ruff, mypy, pytest, pytest-asyncio, asgi-lifespan.

## Repository layout

| Path | Holds |
|------|-------|
| `pyproject.toml` | deps, `ml` extra, ruff/mypy/pytest config (`pythonpath=["src"]`) |
| `Dockerfile` | 2-stage uv build; `runtime` (slim, default) + documented `runtime-ml` sketch; non-root uid 10001; `/livez` healthcheck; `CMD python -m guardrails_service` |
| `src/guardrails_service/__main__.py` | run entry; sets Windows SelectorEventLoop before uvicorn (psycopg3 async needs it) |
| `src/guardrails_service/main.py` | `create_app()` + lifespan: DB pool, classifier, policy engine, redaction resolver, rules overlay, custom-rule loader, Valkey, policy cache, rate limiter, persist queue, outbox publisher + purger, key-retirement job, JWKS warm |
| `api/check.py` | **`POST /v1/check/input` + `/v1/check/output`** — the xAgent spine |
| `api/health.py` | `/livez`, `/readyz`, `/metrics` (Contract 7) |
| `api/policies.py` | `/v1/policies` CRUD + `/assign` + `/simulate` (WP07) |
| `api/rules.py` | `/v1/rules` custom-rule CRUD (regex / classifier-threshold) |
| `api/violations.py` | `GET /v1/violations` (keyset-paginated, RLS, redaction-safe — frontend dep) |
| `api/redaction_keys.py` | `POST /v1/redaction-keys/rotate` (tenant:admin) |
| `core/` | `auth.py` (dual-mode JWT + revocation), `errors.py` (Contract 2 envelope), `trace.py` (Contracts 6/8 middleware), `redaction.py` (HMAC tokens + key lifecycle), `config.py`, `logging.py`, `metrics.py`, `valkey.py`, `slo.py` |
| `services/` | `pipeline.py` (decision engine), `classifier.py` (stub/detoxify), `policy_engine.py` (resolution + CRUD), `policy_cache.py` (cache + rate limiter), `rules/` (definitions, registry overlay, custom loader, ReDoS guard) |
| `db/` | `pool.py` (`in_tenant` RLS helper), `outbox.py` (record_check/usage/policy_change + publisher), `persist_queue.py`, `maintenance.py`, `redaction_keys`/retirement |
| `db/migrations/` | `0001__init`, `0002__seed`, `0003__policy_authoring`, `0004__custom_rules`, `0005__hotpath_redaction_lifecycle`, `schema.sql`, `atlas.hcl` |
| `tests/` | ~155 fns; `conftest.py` pins `CLASSIFIER_MODE=stub`; `_fakedb.py` for DB-backed tests |

## Build, test, run

**Host (uv):**
```bash
uv sync --frozen                 # light install (no torch; stub classifier)
uv run pytest                    # ~155 tests; no real DB/Kafka/Auth needed
uv run ruff check . && uv run mypy
uv run python -m guardrails_service          # serves on $PORT (default 8000 host; 8080 in image)
```
`__main__` reads `HOST`/`PORT` env. Tests override the `require_principal` dependency to inject a Principal — no real Auth/JWKS needed.

**Docker / infra/compose:** built as service `guardrails` → image `cypherx/guardrails-service:local`, container `cypherx-guardrails-service`. In-container port **8080**, host map **8086:8080**. `depends_on` redpanda + auth-service (healthy). Healthcheck hits `/livez`.

**Health:** `GET /livez` (process-only — never touches DB/Kafka/classifier; green before Neon wakes), `GET /readyz` (503 unless Postgres + platform-default policy + classifier-ready + rules-registry-consistent; **Valkey is soft — reported, never fails readiness**), `GET /metrics` (Prometheus, scraped on 8080).

## Configuration & secrets

Env via pydantic-settings, **no prefix**, `case_sensitive=false` (Doppler-injected in cluster; **no `.env.example` committed here**). Key vars:
- `DATABASE_URL` — Neon DSN (`grd_user` → schema `guardrails`). `KAFKA_BROKERS`, `VALKEY_URL`.
- `AUTH_JWKS_URL` / `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` (`cypherx-platform`) — Contract 1.
- `REDACTION_HMAC_KEY_PLATFORM` — per-env fallback HMAC key (per-tenant overrides resolved from DB).
- `CLASSIFIER_MODE` = `stub` (default; keyless dev) | `detoxify` (prod; needs `ml` extra + pinned `DETOXIFY_CHECKPOINT`).
- `RATE_LIMIT_ENABLED` (default **off**), `RATE_LIMIT_FAIL_OPEN`, `POLICY_CACHE_TTL_SECONDS`, `REVOCATION_CHECK_ENABLED`, `REVOCATION_KEY_PREFIX` (`cypherx:rev:` — MUST match all services), `OUTBOX_PURGE_ENABLED`, `REDACTION_KEY_GRACE_DAYS` (30), `CUSTOM_RULES_MAX`, `USAGE_COST_PER_RULE_USD`, `SIMULATION_RATE_LIMIT_PER_HOUR`.
- Mock toggle: `CLASSIFIER_MODE=stub` is the keyless default. With no DB pool the whole DB-backed surface degrades gracefully (built-in default policy, empty lists, 503 on writes) so unit/local tests run with zero infra.

### Additive safety upgrades (WP08 — all flagged, default = today's behaviour)

- **Real classifier seam** — `CLASSIFIER_MODE=llms_gateway` (any value ≠ `stub`/`detoxify`) wraps the stub with a confidence-banded cascade (`services/classifier_client.py`) that calls llms-gateway `POST /v1/classify` ONLY for the uncertain stub band `[classifier_escalate_low=0.30, classifier_escalate_high=0.85)` (latency guard); confidently benign/toxic short-circuits with no round-trip; any remote error/timeout (`classifier_remote_timeout_seconds=0.045`) falls back to the stub (fail-soft). Default `stub` => no network, byte-identical. Keys: `LLMS_GATEWAY_URL`, `CLASSIFIER_REMOTE_MODEL` (`safety-default`), `CLASSIFIER_REMOTE_THRESHOLD`.
- **PII via Presidio** — `GUARDRAILS_PII_PRESIDIO` (default **off**) + optional `pii` extra (`presidio-analyzer`). When on, Presidio analyze runs BEFORE the regex/HMAC redaction and its spans are UNIONed into the PII detectors (same `[REDACTED:cat:hex8]` token). Missing dep => graceful regex-only fallback. `PRESIDIO_SCORE_THRESHOLD`, `PRESIDIO_ENTITIES`.
- **Prompt-injection defense** — `INJECTION_DEFENSE_ENABLED` (default on, inert without marked spans). Caller marks RAG/tool spans via the new optional body field `untrusted_spans[]`; an injection/jailbreak pattern inside one is spotlighted and escalated to **block** under `INJECTION_SPOTLIGHT_BLOCK_THRESHOLD` (0.5). Additive `metadata.injection`. Benign + trusted-only matches keep today's verdicts.
- **Output groundedness** — `GROUNDEDNESS_ENABLED` (default **off**). On `/v1/check/output`, a heuristic (or `GROUNDEDNESS_BACKEND=llms_gateway`) entailment proxy over `input_text` + the new optional `grounding[]` field flags low-support output (`< GROUNDEDNESS_MIN_SCORE=0.4`) by escalating an `allow` to a `warn` REVIEW signal (never blocks on its own). Additive `metadata.groundedness`.
- **Response superset** — `/v1/check/*` now also returns `confidence` (float, default 1.0) and optional `metadata` (omitted when empty). Existing fields unchanged.
- **eval/** — `eval/golden_set.jsonl` (benign/toxic/jailbreak/injection/PII) + `eval/run_eval.py` reporting precision/recall/F1 + p50/p99 vs the 30/50ms-in, 60/100ms-out SLOs (`uv run python eval/run_eval.py [--json]`); CI-guarded by `tests/test_eval_harness.py`.

## Contracts & cross-repo dependencies

- **Called by xAgent** (INTERNAL mode): service JWT in `Authorization` + agent JWT in `X-Forwarded-Agent-JWT`; service token `on_behalf_of` MUST equal the forwarded `agent_id` (Contract 12). Both verified; scope `guardrails:check` required.
- **Calls Auth**: JWKS (`/.well-known/jwks.json`, RS256, 5-min key cache); mirrors the shared revocation kill-switch in Valkey (`cypherx:rev:jti|kid|agent:`).
- **Contracts honoured**: 1 (JWT), 2 (`{error:{code,message,details?,request_id,trace_id,timestamp}}`), 5 (Kafka envelope, `partition_key=tenant_id`), 6/8 (trace context, `traceparent`/`X-Request-ID`), 7 (health), 13 (tenant/RLS), 19/19.1 (usage metering). Golden suite + jailbreak-leak patterns live in `contracts/guardrails/`.
- **Kafka topics produced** (via outbox → `cypherx.guardrails.*`): `violation.detected` (schema in `contracts/kafka/events/guardrails.violation.detected.schema.json`; required field `policy`), `usage.recorded`, `policy.changed`. DLQ after 10 attempts (`<topic>.dlq`).
- **DB owned**: schema `guardrails`, role `grd_user` (LOGIN, not superuser, no BYPASSRLS). Tables: `rules` (mixed-scope: NULL platform + tenant custom), `policies` (mixed), `agent_policies`, `violations` (append-only), `tenant_redaction_keys`, `outbox` (RLS DISABLED — internal cross-tenant queue), `policy_audit`.

### The check endpoints (what xAgent sends/receives)

Request body (identical for input/output; `extra="forbid"`): `{text, input_text?, task_id?, policy_set_id?, mode?}`. **`input_text`** matters only on output (distinguishes "email not in input" from "user echoed own email"). **Identity (`tenant_id`/`agent_id`) and correlation (`trace_id`/`request_id`/`check_id`) come from the JWT + trace headers ONLY — supplying any of `{agent_id, tenant_id, trace_id, span_id, request_id, check_id}` in the body → 400 VALIDATION_ERROR.** `mode='async'` → 422.

Response (always **HTTP 200**, even on block): `{decision, processed_text?, violations[], check_id, duration_ms, trace_id}`. `processed_text` is populated only when the decision is `redact`. `violation.matched` is always safe (redaction token for PII, ≤64-char truncation otherwise). **The CALLER (xAgent) turns `decision='block'` into a 422 GUARDRAIL_VIOLATION** — this service does not 4xx a block.

## Invariants & guards (do NOT break)

- **Endpoints ALWAYS return 200**, including `decision='block'`. Do not make the service raise on a block.
- **Identity from the token, never the body** (Contract 13). The reserved-field 400 guard and JWT-only Principal are load-bearing.
- **Raw PII never leaves the pipeline.** `violation.matched` and `violations.matched_text` store only the `[REDACTED:cat:hex8]` token (HMAC-SHA256 over `tenant_id:matched_text`, first 8 hex) or a ≤64-char truncation. Detection runs on the ORIGINAL text (not progressively-redacted text) so tokens are not re-matched.
- **Decision precedence** BLOCK > REDACT > WARN > ALLOW; rules run lexicographic by `rule_id`; **short-circuit on first block** (Audit #2). Per-rule timeout → `default_fail_mode` (`closed`=block).
- **Empty tenant policy is honoured as allow** — the built-in default (all 11 rules) only stands in when the resolver yields nothing at all (no row / DB down). Do not re-substitute the default for a deliberately-empty policy.
- **RLS is real**: `grd_user` is not superuser; every tenant query runs through `in_tenant()` (`SET LOCAL app.tenant_id`). Platform (`tenant_id IS NULL`) policies/rules are immutable from the tenant API (RLS WITH CHECK).
- **Fail postures**: policy cache & redaction-key resolve & revocation = **FAIL-OPEN** (latency/defense-in-depth, never block a legit check); rate limiter when ENABLED = **FAIL-CLOSED** (429); persistence is post-response (a dropped audit write degrades metering, not safety).
- **JWT**: RS256 only; `iss`/`aud`/`exp` enforced; aud accepts platform audience OR `*` (service tokens). Append-only outbox & policy/rule version chains — never mutate published rows.
- **PLATFORM_DEFAULT_POLICY_ID = `00000000-0000-0000-0000-0000000d0001`** must stay in lockstep between the seed and `services/policy_engine.py`. `REVOCATION_KEY_PREFIX` must match all four services.

## Gotchas & current status

- **Stale CLAUDE.md replaced.** Anything referencing Qdrant, llm-router, `GR_*` env, ports 908x, `sanitize`, or "no Kafka" is the OLD design — gone on `development`.
- **Migration README is mislabeled.** `db/migrations/README.md` lists 4 files and labels `0004` as `hotpath_redaction_lifecycle`; on disk there are **5** migrations — `0004__custom_rules.sql` is separate and the hotpath/redaction-lifecycle file is `0005`. Trust the files, not the README table. (Migration 0005's header comment also calls itself "0004" internally — cosmetic.)
- **`tenant_redaction_keys.key_ref` scheme**: `0001` constrained it to `secretsmanager:%`; `0005` widens to `env:`/`sealed:`/legacy `secretsmanager:`. `sealed:`/`secretsmanager:` are NOT wired first cycle — they resolve to the platform key (fail-soft). `env:NAME`→env var; bare `env:`→platform key.
- **Detoxify is opt-in & graceful.** Default stub needs no torch. If `CLASSIFIER_MODE=detoxify` but the dep/checkpoint is missing, `build_classifier` falls back to the stub (logged); stub is ALWAYS ready so `/readyz` never hard-fails on a missing model.
- **`fail_mode_override`** is stored/audited/exposed in simulation trace AND (since the WP08 additive update) honoured on the LIVE check path too, gated by `LIVE_FAIL_MODE_OVERRIDE_ENABLED` (default **on**; set off to revert to each rule's `default_fail_mode`). The applied posture is echoed in the additive `metadata.fail_mode_applied`.
- **`policy_set_id`** in the request body is accepted but resolution is via the agent/tenant/platform chain (the field is not the primary selector first cycle).
- **Windows dev**: psycopg3 async requires SelectorEventLoop — set in both `__main__.py` and `main.py`; `__main__` runs uvicorn with `loop="none"` on win32.
- **No `.env.example`** committed (secrets via Doppler). `README.md` is the default GitLab template — ignore it.
