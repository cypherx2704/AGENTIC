# Testing strategy

> How cypherx-a1 proves correctness without a network: a network-free pytest suite (DB pool nulled, downstreams `respx`-mocked, auth bypassed via `dependency_overrides`), a **mandatory** cross-tenant-denial test gated on a throwaway Postgres, connector contract tests, extraction golden sets, a retrieval-relevance eval, MCP manifest conformance against the Contract-4 schema, input-schema validation tests, and an all-keyless local run.

This document is authoritative for the cypherx-a1 product service (`src/cypherx_a1`) **and** the stateless MCP facade (`mcp-eng-memory/`). It describes what is tested, where the tests live, and the invariants each layer guards. Test code paths quoted below are real and current as of 2026-06-14.

---

## 1. Goals & principles

cypherx-a1 is a CypherX **consuming app** (peer of `xAgent/ax-1`), so its test strategy mirrors the xAgent ax-1 / SharedCore Python template. Five principles govern the suite:

| Principle | What it means in practice |
| --- | --- |
| **Default tests are network-free** | `uv run pytest` boots the app with **no Postgres, no Kafka, no Valkey, no Auth/JWKS, no SharedCore** reachable. Every downstream is either lazy (never opened) or `respx`-mocked. The suite runs offline and deterministically in CI. |
| **Identity is injected, never minted** | Auth is bypassed with `app.dependency_overrides[require_principal]`. Tests never hit Auth, never verify a real JWT, and never depend on JWKS. The injected `Principal` carries `tenant_id`/`agent_id`/`scopes` — exactly the surface the handlers consume. |
| **Tenant isolation is proven, not asserted by inspection** | One **mandatory** DB-gated test (`tests/test_rls_cross_tenant.py`) applies the real init migration to a throwaway Postgres and proves a tenant literally cannot see another tenant's rows under `ENABLE` + `FORCE` RLS. Every new tenant-scoped table must be added to it. |
| **Contracts are validated against the contract, not a copy** | The MCP manifest is validated against the live `contracts/mcp/manifest.schema.json` (Draft 2020-12). The Contract-2 error envelope, Contract-7 health endpoints, and `extra="forbid"` body guards are exercised as wire behaviour. |
| **Keyless/mock parity** | The keyless local profile (`CONNECTOR_MODE=mock`, upstream `MOCK_PROVIDERS`/`MOCK_EMBEDDINGS`) is the same code path the connector and extraction unit tests exercise — so "it passes pytest" and "it runs keyless in compose" are the same guarantee. |

### Tooling

| Concern | Tool | Config |
| --- | --- | --- |
| Test runner | `pytest>=8.3` | `[tool.pytest.ini_options]` in `pyproject.toml` |
| Async tests | `pytest-asyncio>=0.24`, **`asyncio_mode = "auto"`** | `async def test_*` needs no `@pytest.mark.asyncio` |
| HTTP mocking | `respx>=0.21` | mocks `httpx` calls to SharedCore at the transport layer |
| Lifespan in tests | `fastapi.testclient.TestClient` (sync) / `asgi-lifespan` (async) | `with TestClient(app) as c:` runs startup/shutdown |
| Lint | `ruff>=0.7`, **line-length 110** | `[tool.ruff]` |
| Types | `mypy>=1.13` | `[tool.mypy]`, `mypy_path = "src"` |

Run the full quality gate exactly as CI does:

```bash
uv sync
export SERVICE_BOOTSTRAP_SECRET=local-dev-cypherxa1-secret   # required, no default
uv run pytest                                # network-free, both packages
uv run ruff check src tests && uv run mypy
```

---

## 2. The network-free harness (product service)

### 2.1 How the app boots with no infrastructure

The entire trick lives in `tests/conftest.py`. Three environment toggles are set **before** the app imports `get_settings()` (load order matters — `SERVICE_BOOTSTRAP_SECRET` has no default and fails fast):

```python
# tests/conftest.py
os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "test-secret")
os.environ.setdefault("DB_POOL_OPEN_AT_STARTUP", "false")    # DB pool created but NOT opened
os.environ.setdefault("OUTBOX_PUBLISHER_ENABLED", "false")   # aiokafka relay disabled
os.environ.setdefault("REVOCATION_CHECK_ENABLED", "false")   # Valkey revocation mirror off
```

| Toggle | Effect | Why it makes the suite offline |
| --- | --- | --- |
| `DB_POOL_OPEN_AT_STARTUP=false` | The `psycopg_pool.AsyncConnectionPool` is constructed but its `open()` is deferred. No socket to Neon at startup. | `/readyz` would 503, but the API-contract tests short-circuit **before** any DB access (`extra="forbid"` 422, route 404, health). |
| `OUTBOX_PUBLISHER_ENABLED=false` | The `aiokafka` outbox publisher loop never starts. | No Kafka broker required; `db/outbox.py` envelope construction is unit-tested separately. |
| `REVOCATION_CHECK_ENABLED=false` | The Valkey revocation mirror is not consulted in `core/auth.py`. | No Redis/Valkey required; revocation is a **soft** dependency by design. |

The downstream SharedCore clients (`services/llms_client`, `guardrails_client`, `rag_client`, `memory_client`, `service_token`) are **lazy** — they open no `httpx` connection until first used. A test that never reaches the copilot/retrieval path therefore needs no `respx` mock at all.

### 2.2 The two shared fixtures

```python
# tests/conftest.py
@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id="00000000-0000-0000-0000-0000000000aa",
        agent_id="11111111-1111-1111-1111-111111111111",
        scopes=["cypherxa1:query", "cypherxa1:ingest"],
        raw_token="agent.jwt.token",
    )

@pytest.fixture
def client(principal: Principal) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

- `create_app()` is the **same** factory used in production (`cypherx_a1.main:create_app`); nothing about the app graph is faked except the auth dependency and the (un-opened) pool.
- `app.dependency_overrides[require_principal]` replaces the JWKS-verifying auth dependency with a constant `Principal`. This is the only sanctioned way to "log in" in a test — it exercises every handler downstream of auth without a token, JWKS fetch, or clock.
- The override is **cleared in teardown** so fixtures don't bleed across tests.
- Identity comes from the `Principal` only — never from a request body. Tests that try to smuggle `tenant_id` into a body are expected to be rejected (see §6).

### 2.3 Mocking SharedCore with respx

When a test must reach the copilot or retrieval orchestrator (which call llms-gateway / guardrails / rag / memory over `httpx`), the calls are intercepted with `respx`. The pattern:

```python
import respx, httpx

@respx.mock
async def test_copilot_answer_is_cited(client):
    respx.post("http://guardrails/v1/check/input").mock(
        return_value=httpx.Response(200, json={"decision": "allow"}))
    respx.post("http://llms/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "id": "llm_abc", "choices": [{"message": {"content": "Because X. [1]"}}],
            "usage": {"cost_usd": 0.0001}}))
    respx.post("http://guardrails/v1/check/output").mock(
        return_value=httpx.Response(200, json={"decision": "allow"}))
    ...
```

Endpoint paths in `respx` mocks must match the **real** SharedCore routes — the copilot/extraction code calls `POST /v1/chat/completions`, `POST /v1/check/input`, `POST /v1/check/output`, `POST /v1/kbs/{id}/query`. Mocking a wrong path is a test bug that surfaces as an unmatched-route error from `respx`, which is the intended fast failure.

**Guardrails fail-closed is testable here:** a `respx` mock returning `{"decision": "block"}` on `/v1/check/output` must drive the handler to a **422 `GUARDRAIL_VIOLATION`** — never a degraded answer.

---

## 3. The mandatory cross-tenant-denial test (Contract 13)

This is the single most important correctness test in the repo and is **mandatory**: every new tenant-scoped table must be covered, per `CLAUDE.md`. It lives in `tests/test_rls_cross_tenant.py`.

### 3.1 What it proves

The `cypherx_a1` schema uses `ENABLE ROW LEVEL SECURITY` **plus** `FORCE ROW LEVEL SECURITY`, so the RLS policy applies **even to the table owner**. The test:

1. Connects to a **throwaway** Postgres via `CYPHERXA1_TEST_DSN` (an owner/admin DSN — the init migration creates extensions + roles).
2. Applies the real init migration verbatim: `db/migrations/20260614_0001__init.sql`.
3. Inserts one `entities` row per tenant, each **inside its own tenant context** so the policy's `WITH CHECK` passes:
   ```python
   await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
   ```
4. Sets `app.tenant_id` to tenant **A** and asserts:
   - tenant A sees **exactly one** row (its own), and
   - a direct `WHERE tenant_id = <B>::uuid` query returns **zero** rows — tenant A cannot see tenant B's data even when asking for it by id.

```python
TENANT_A = "00000000-0000-0000-0000-0000000000aa"
TENANT_B = "00000000-0000-0000-0000-0000000000bb"

assert visible == 1, f"tenant A must see exactly its own row, saw {visible}"
assert leaked  == 0,  "tenant A must NOT see tenant B's rows (cross-tenant denial)"
```

### 3.2 How it is gated

The test is **skipped** unless a DSN is provided — it never runs in the default network-free suite:

```python
DSN = os.environ.get("CYPHERXA1_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set CYPHERXA1_TEST_DSN (owner DSN) to run RLS tests")
```

Run it against a disposable database (local container or a throwaway Neon branch):

```bash
export CYPHERXA1_TEST_DSN='postgresql://owner:pw@localhost:5432/throwaway?sslmode=disable'
uv run pytest tests/test_rls_cross_tenant.py -v
```

### 3.3 The rule for new tables

Every table in the `cypherx_a1` schema is tenant-scoped and RLS-guarded **except `outbox`** (which has no RLS by design — it is a cross-tenant publish queue with isolation carried in the payload). The full set: `entities`, `edges`, `identities`, `raw_events`, `connectors`, `connector_secrets`, `sync_cursors`, `extraction_jobs`, `citations`, `resource_acls`, `rag_kbs`.

> **Guard:** When you add a tenant-scoped table, you MUST extend `tests/test_rls_cross_tenant.py` with an insert-per-tenant + cross-tenant-invisibility assertion against it. The test currently exercises `entities` as the canonical example; a missing table is a missing isolation proof, not an oversight.

---

## 4. API-layer contract tests

`tests/test_api_contracts.py` exercises the wire behaviour that must hold for **every** request, all of it short-circuiting before any DB access:

| Test | Endpoint | Asserts |
| --- | --- | --- |
| `test_livez_ok` | `GET /livez` | 200, `status == "ok"` (process-only liveness, Contract 7) |
| `test_metrics_exposed` | `GET /metrics` | 200, Prometheus text contains the `cypherxa1_` prefix (or empty before any metric fires) |
| `test_unknown_route_renders_contract2_envelope` | `GET /v1/does-not-exist` | 404 with a **Contract-2 error envelope**: `error` object carrying at least `{code, message, request_id, trace_id, timestamp}` |
| `test_copilot_rejects_reserved_and_unknown_body_keys` | `POST /v1/copilot/ask` body `{"question":"hi","tenant_id":"x"}` | **422 `VALIDATION_ERROR`** — `extra="forbid"` rejects identity/unknown keys **before** the handler touches the DB |
| `test_copilot_requires_question` | `POST /v1/copilot/ask` body `{}` | 422 (missing required field) |
| `test_request_id_echoed` | `GET /livez` with `X-Request-ID: abc-123` | response `x-request-id` header echoes `abc-123` |

The reserved-key guard is the wire enforcement of the platform invariant **"identity from JWT only"**: a client cannot pin a `tenant_id`/`agent_id` in the body because the request models declare `extra="forbid"`. This is verified as a real 422, not by reading the model.

---

## 5. Connector contract tests (GitHub-first)

`tests/test_github_connector.py` validates the **connector SPI** (`connectors/base.py`) — the source-agnostic contract every connector implements. The pipeline is written once against this interface; a new market tool is one SPI subclass + one registry row with zero changes to normalization/storage/retrieval/copilot. Tests run in **mock mode** (`CONNECTOR_MODE=mock`, bundled GitHub fixtures) — fully keyless, no network.

The SPI under test (`Connector` ABC):

| Method | Mode | Contract |
| --- | --- | --- |
| `full_sync(*, stream, cursor) -> SyncBatch` | PULL | resumable backfill; `SyncBatch.records`, `.next_cursor`, `.done` |
| `incremental_sync(*, stream, cursor) -> SyncBatch` | PULL | delta sync since cursor |
| `verify_signature(*, headers, body) -> bool` | PUSH | HMAC-SHA256 webhook signature |
| `parse_webhook(*, event, payload) -> list[CanonicalRecord]` | PUSH | normalize one delivery into canonical records |

| Test | Guards |
| --- | --- |
| `test_registry_knows_github` | `"github" in supported_kinds()`; `get_connector("github", ...)` returns a `GitHubConnector` (registry wiring) |
| `test_full_sync_mock_returns_canonical_records` | the fixtures normalize into the canonical model; node kinds include `{repo, service, person, pr, ticket}`; **edge rels include `{owns, authored, depends_on, part_of}`** so `who_owns` / `what_breaks` work **keyless** without an LLM |
| `test_records_are_idempotent_by_content_sha` | `content_sha` is deterministic across two syncs — the re-ingest dedup key. Idempotent landing in `raw_events` depends on this |
| `test_pr_record_has_rag_doc_and_author_edge` | a PR record carries a RAG doc bound to KB **`eng-code`**, plus `authored` + `part_of` edges (the graph/RAG split is wired at the connector) |
| `test_verify_signature_roundtrip` | a correct `sha256=…` HMAC over the body verifies; a wrong signature and a missing header both fail (fail-closed webhook auth) |
| `test_parse_webhook_pull_request` | a `pull_request` delivery parses into exactly one `CanonicalRecord` with a `pr` node whose `natural_key == "acme/web#7"` |

> The keyless fixtures deliberately ship explicit `owns`/`depends_on` edges. This is what lets the demo answer "who owns X" / "what breaks if I change X" **without** an LLM provider; extraction (§7) only **enriches** when a real provider is configured. The connector tests are the proof that the deterministic graph alone satisfies the headline queries.

### 5.1 The contract every new connector inherits

When you add a connector (Jira, Slack, …), its test file must assert the same four SPI behaviours against that source's fixtures: a mock `full_sync` yields canonical records, `content_sha` is stable, the webhook signature round-trips, and a representative webhook payload parses. Anything the GitHub connector proves, a new connector must prove for itself — the pipeline trusts the SPI, not the source.

---

## 6. Input-schema & validation tests

Validation is enforced at two independent layers, both tested:

### 6.1 Product service — pydantic `extra="forbid"`

The product API request models (`models/api.py`) declare `extra="forbid"`. A reserved or unknown body key produces a **422 `VALIDATION_ERROR`** before the handler runs (see §4). This is the product-side anti-spoof guard for Contract-13 identity.

### 6.2 MCP facade — dependency-free input-schema validator

The MCP server validates each tool's `args` against that tool's `input_schema` from the committed `manifest.json`, using a **dependency-free** validator in `mcp-eng-memory/src/mcp_eng_memory/services/manifest.py::validate_input`. It is intentionally minimal (type/`required`/`minLength`/`minimum`/`maximum`/`additionalProperties`) and raises `SchemaViolation(pointer, message)` carrying a **JSON Pointer** to the offending field.

Behaviours the validator guarantees (and the tests assert):

| Rule | Code | Test |
| --- | --- | --- |
| Unknown tool → violation at `/` | `tools_by_name().get(tool_name) is None` | `test_unknown_tool_404` (dispatch layer → 404) |
| `additionalProperties: false` → reject unexpected keys at `/<key>` | the `additional is False` loop | `test_additional_properties_rejected` |
| Missing required field → violation at `/<field>` | the `required` loop | `test_input_schema_validation_pointer` (asserts `pointer == "/target"`) |
| `type: string` + `minLength` | `_validate_value` | covered via manifest shapes |
| `type: integer` with `minimum`/`maximum`; **`bool` rejected** as a non-integer | `_validate_value` (`isinstance(value, bool)` guard) | regression-critical: `max_hops` is an `integer` 1–6 |
| `type: number`; `bool` rejected | `_validate_value` | — |

The `test_input_schema_validation_pointer` test is the precise contract: a missing `target` for `who_owns` returns **422** with `error.details.pointer == "/target"`. The JSON Pointer is part of the wire contract for callers, so it is asserted literally.

> **Why a hand-rolled validator?** `mcp-eng-memory/` is a lean, DB-free, Kafka-free package; pulling in a full JSON-Schema engine at runtime would be dead weight for the handful of scalar constraints the tools use. The full Draft-2020-12 validation happens once, in CI, against the manifest *file* (§8) — runtime only needs the scalar subset.

---

## 7. Extraction golden sets

The knowledge-extraction engine (`extraction/extractor.py`) asks the llms-gateway (with `response_format={"type":"json_object"}`) to surface relationships the deterministic ingest can't see — `depends_on`, `decided_in`, `caused`, `resolved`, `expert_in`, `mentions`. Two things must be tested deterministically: **parsing/validation of the LLM JSON** and **idempotency/cost discipline**. Neither requires a live model.

### 7.1 The parser golden set (`_parse_edges`)

`_parse_edges(content)` is a pure function — the ideal golden-set target. It is **tolerant** (a non-JSON / malformed response yields `[]`, so the job is recorded once and never retried forever) and **strict** (it only emits edges whose `rel ∈ _EXTRACTABLE_RELS` and `target_kind ∈ _TARGET_KINDS`, with a clamped `confidence ∈ [0,1]`). A golden set pins these cases:

| Golden input (LLM `content`) | Expected `_parse_edges` output | What it locks |
| --- | --- | --- |
| `'{"edges":[{"rel":"depends_on","target_kind":"service","target_key":"acme/payments","confidence":0.9,"evidence":"…"}]}'` | one normalized edge | the happy path |
| `'{"edges":[]}'` | `[]` | "no edges" is valid, not an error |
| `'sorry, I cannot'` (non-JSON, mock-provider output) | `[]` | keyless/mock safety — extraction never crashes on a canned completion |
| edge with `rel:"owns"` | dropped | `owns`/`authored`/`reviewed`/`part_of` come from **deterministic ingest**, never the LLM |
| edge with `target_kind:"galaxy"` | dropped | target-kind allow-list |
| `confidence: 5` / `confidence:"high"` | clamped to `1.0` / defaulted to `0.5` | confidence bounding |
| `{"edges":"nope"}` / `[...]` (not a dict) | `[]` | shape tolerance |

These run network-free: call `_parse_edges` directly on each string and compare to the expected list. They are the regression net for the most fragile surface — what an LLM returns.

### 7.2 Idempotency & cost ledger

`run_extraction` keys each job on `(tenant_id, node_id, content_sha, extractor_version)` in `extraction_jobs` (re-ingest never re-spends), passes an `Idempotency-Key` on every gateway chat call (`_idem_key(...)`), and records the gateway's `llm_call_id` + `cost_usd` **verbatim** (Contract 19 — cypherx-a1 never rewrites the gateway's cost). Tests (with the llms-gateway `respx`-mocked) assert:

- a node already at the current `extractor_version` is **skipped** (no second gateway call) — the idempotency key in the request matches `_idem_key`;
- the recorded `cost_usd` equals the gateway's reported value (no rewrite);
- a model/prompt bump (`extractor_version` change) **supersedes** prior edges bitemporally via `graph_repo.supersede_extracted_edges` rather than duplicating;
- one node's failure increments `stats.failed` and does **not** abort the pass (`metrics.extraction_jobs_total.labels("failed")`).

> In keyless mode (`MOCK_PROVIDERS`) the gateway returns a canned completion with no useful JSON, so `_parse_edges` yields `[]` and the job is simply recorded. The extraction tests therefore have the same shape whether or not a provider is configured — the golden set is the source of truth.

---

## 8. MCP manifest conformance (Contract 4)

`mcp-eng-memory/tests/test_manifest.py` validates that the served manifest is a valid Contract-4 MCP manifest, **against the live contract schema**, not a vendored copy:

```python
_CYPHER_ROOT = pathlib.Path(__file__).resolve().parents[4]            # .../Cypher
_SCHEMA = _CYPHER_ROOT / "contracts" / "mcp" / "manifest.schema.json"
```

| Test | Asserts |
| --- | --- |
| `test_manifest_served_with_etag` | `GET /manifest` → 200 with a strong **`ETag`**; body `name == "mcp-eng-memory"`; tool set ⊇ `{who_owns, what_breaks_if_changed, experts_on}`; an `If-None-Match` with that ETag → **304** |
| `test_manifest_conforms_to_contract4_schema` | the served manifest validates with **`Draft202012Validator(schema)`** against `contracts/mcp/manifest.schema.json` — zero errors. If `jsonschema` is unavailable, it falls back to a structural `required`-keys check |
| `test_required_scopes` | `required_scopes == ["tool:invoke", "tool:mcp-eng-memory:invoke"]` (coarse + fine) |

### 8.1 The ETag / 304 mechanism

The manifest is a committed source-of-truth file (`MANIFEST_PATH`, default `mcp-eng-memory/manifest.json`), loaded once via `@lru_cache` (`load_manifest`). The ETag is a **content-addressed SHA-256** over the canonical JSON:

```python
def manifest_etag(manifest):
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return '"' + hashlib.sha256(canonical).hexdigest() + '"'
```

The 304 path is part of the contract (caching for AI agents that poll the manifest), so it is tested explicitly.

### 8.2 What the manifest declares (registered as `mcp-eng-memory@1.0.0`)

The seven read-only, source-cited tools, each with an `input_schema` the validator enforces:

| Tool | Required arg(s) | Notable constraints | Backend route / mode |
| --- | --- | --- | --- |
| `who_owns` | `target` (string, minLength 1) | `additionalProperties: false` | `POST /v1/graph/who-owns` |
| `why_built` | `feature` (string) | — | graph |
| `what_breaks_if_changed` | `target` (string) | `max_hops` integer 1–6 (default 3) | graph |
| `experts_on` | `topic` (string) | — | graph |
| `graph_neighbors` | `target` (string) | `max_hops` integer 1–4 (default 2) | graph |
| `incident_root_cause` | `incident` (string) | timeout 60s (LLM) | copilot |
| `how_does_x_work` | `topic` (string) | timeout 60s (LLM) | `POST /v1/copilot/ask` |

> **Guard:** the committed `manifest.json` is the single source of truth. Changing a tool's name, args, or scopes is a manifest edit that must keep `test_manifest_conforms_to_contract4_schema` green and bump `mcp-eng-memory` versioning if it is breaking. The runtime validator (`validate_input`) reads these exact schemas, so manifest and dispatch stay in lockstep.

---

## 9. MCP invoke / dispatch tests

`mcp-eng-memory/tests/test_invoke.py` exercises `POST /mcp/v1/invoke` — dispatch, input-schema validation, the scope guard, and the tool guard — all network-free. The facade is **stateless** (no DB/Kafka/outbox); its `conftest.py` swaps the real `BackendClient` for a `FakeBackend` and overrides `require_principal`, so neither Auth/JWKS nor the cypherx-a1 backend is needed:

```python
# mcp-eng-memory/tests/conftest.py
class FakeBackend:
    async def graph(self, path, body, *, agent_jwt):
        self.calls.append((path, body))
        return {"items": [...], "citations": [{"kind": "entity", "title": "acme/payments"}]}
    async def ask(self, question, *, agent_jwt):
        self.calls.append(("/v1/copilot/ask", {"question": question}))
        return {"answer": "because X", "citations": [{"kind": "chunk", "title": "PR 101"}]}
```

| Test | Asserts |
| --- | --- |
| `test_who_owns_dispatches_to_backend` | `who_owns` → 200; response is **cited** (`body["citations"]` non-empty); the facade called the backend at **`/v1/graph/who-owns`** |
| `test_how_does_x_work_uses_copilot` | `how_does_x_work` → 200 with a non-empty `output.answer`; the facade called **`/v1/copilot/ask`** |
| `test_unknown_tool_404` | an unregistered tool (`rm_rf`) → **404** |
| `test_input_schema_validation_pointer` | missing required `target` → **422**, `error.details.pointer == "/target"` |
| `test_additional_properties_rejected` | an extra `evil` key → **422** (`additionalProperties: false`) |
| `test_fine_scope_required` | a principal holding only the coarse `tool:invoke` scope (missing `tool:mcp-eng-memory:invoke`) → **403** before any backend call |

The `FakeBackend.calls` list is the assertion surface for **routing**: each tool must dispatch to the right backend endpoint. This is what proves the facade is a thin, correct proxy — graph tools to `/v1/graph/*`, LLM tools to `/v1/copilot/ask` — and that it carries no tenant logic of its own (the backend re-verifies the forwarded agent JWT and enforces RLS).

> The `BackendClient` itself (`services/backend.py`) maps backend HTTP status to Contract-2 `ApiError`s: `401/403` → `FORBIDDEN` ("Backend rejected the forwarded agent token"), other `>=400` and transport errors → `SERVICE_UNAVAILABLE`. These mappings are covered by `respx`-mocking the backend at the `BackendClient` level when exercised directly.

---

## 10. Retrieval-relevance eval

The hybrid retrieval orchestrator (`retrieval/orchestrator.py`) fuses three legs and must be evaluated for **relevance**, not just "it returns something". The three legs:

| Leg | Source | Notes |
| --- | --- | --- |
| **graph** | `graph_repo.find_entities` (FTS/keyword + natural-key over the app-owned graph) | the crown-jewel graph, never in RAG |
| **rag-dense** | `RagClient.query` across the per-tenant RAG KBs | the embeddings leg; `top_k` bounded |
| **keyword** | `graph_repo.keyword_search` (a second tsvector pass) | the BM25-ish leg — **RAG ships dense-only first cycle, so cypherx-a1 owns keyword** |

The legs are fused with **reciprocal-rank fusion (RRF)**: `score += 1 / (k + rank)` with `k = retrieval_rrf_k`. RAG hits are mapped back to their originating graph entity via the `doc_id` citation link (`ingest_repo.entities_for_docs`) so a chunk and its entity **reinforce** each other — the whole point of hybrid.

### 10.1 The eval harness

Build a small, committed **labelled fixture set**: a fixed tenant graph (the GitHub mock fixtures) + a set of `(query, expected_entity_keys)` judgments. With the graph/keyword legs running against a throwaway Postgres (or unit-tested against repo fakes) and the RAG leg `respx`-mocked to return canned dense hits, assert relevance with rank-aware metrics:

| Metric | What it checks | Threshold guidance |
| --- | --- | --- |
| **Recall@k** | the expected entity appears in the top-`retrieval_context_max_chunks` | must be 1.0 for the headline demo queries (`who owns acme/payments`, `what depends on acme/payments`) |
| **MRR** | the first relevant result's reciprocal rank | guards that RRF ranks the right entity highly, not merely present |
| **Citation completeness** | every returned `EvidenceItem` becomes a `Citation` (answers are **never** uncited) | invariant — `RetrievalResult.citations()` length == items length |
| **Reinforcement** | a query with both a matching entity and a matching chunk ranks the **fused** entity above either leg alone | proves the `doc_id`→entity merge fires |

`RetrievalResult.used` exposes per-leg hit counts (`{"graph": n, "keyword": n, "rag": n}`) — the eval can assert that all three legs contributed for a query designed to hit all three, catching a silently-dead leg (e.g., a KB-resolution regression that drops the RAG leg to zero).

### 10.2 RAG-consumption constraints the eval must respect

These are platform invariants and the eval fixtures must honour them (they are how cypherx-a1 stays within the RAG `/v1` contract):

- `top_k <= 100`, `ef_search <= 500` on RAG queries; KBs are queried with their **pinned** embedding model (never the repointable `embed` alias) recorded in `rag_kbs`.
- **`@>`-containment filters only** — time/range filtering is done **app-side** (ISO strings + app-side range filtering), never pushed as RAG operators.
- The **graph never enters RAG**; `rag.chunks` are opaque text+metadata. The eval asserts the graph leg reads from `cypherx_a1.entities`, not from RAG.
- A `RagQueryResult.forbidden` (per-KB ACL denial) is **skipped**, not fatal — the eval includes a forbidden-KB case to prove the orchestrator degrades gracefully (`if res.forbidden: continue`).

---

## 11. Keyless / mock local runs

The keyless profile is both the demo path and a test invariant: the whole slice runs **fully offline**.

| Surface | Keyless setting | Behaviour |
| --- | --- | --- |
| GitHub connector | `CONNECTOR_MODE=mock` | bundled fixtures; `full_sync` yields canonical records with explicit `owns`/`depends_on` edges (graph queries work with no LLM) |
| llms-gateway | upstream `MOCK_PROVIDERS=true` | canned completions; extraction `_parse_edges` yields `[]` and records the job (no spend, no crash) |
| RAG embeddings | upstream `MOCK_EMBEDDINGS=true` | deterministic mock vectors; dense leg returns stable hits |
| Guardrails | `CLASSIFIER_MODE=stub` (upstream) | allow-by-default screening; the fail-closed path is exercised by mocking a `block` decision |

Bring the keyless slice up in compose:

```bash
# from infra/compose/  (Postgres is EXTERNAL Neon; redpanda/valkey/minio are containers)
docker compose --profile migrate up migrate                 # creates schema cypherx_a1 + role cxa1_user, seeds auth.service_acl
docker compose up -d --build cypherx-a1 mcp-eng-memory       # host 8093 / 8094
```

Smoke the running slice (keyless, end-to-end):

```bash
# product service health (Contract 7)
curl -s localhost:8093/livez
curl -s localhost:8093/readyz       # 200 once Postgres reachable + Auth JWKS warm

# MCP manifest (Contract 4) — same bytes the conformance test validates
curl -s localhost:8094/manifest | jq '.name, (.tools | map(.name))'
```

Because the connector/extraction/parser unit tests exercise the **same** keyless code paths, a green `pytest` run is strong evidence the keyless compose slice behaves — and vice versa. The migrate job is **idempotent** (re-runnable; exits 0), and `mcp-eng-memory` is **Valkey-free** by design (revocation is enforced at the cypherx-a1 backend it forwards to), so the facade has nothing extra to stand up in a test or a smoke run.

---

## 12. What is intentionally NOT tested in the default suite

| Not in default `pytest` | Why | How it IS covered |
| --- | --- | --- |
| Real Postgres / RLS | network-free default | `tests/test_rls_cross_tenant.py`, gated on `CYPHERXA1_TEST_DSN` (§3) |
| Real Auth / JWKS / token verification | offline | `require_principal` is `dependency_overrides`-injected (§2.2); JWKS verify is unit-tested in `core/auth` with fixed keys |
| Real SharedCore (llms/guardrails/rag/memory) | offline, deterministic | `respx`-mocked at the `httpx` layer (§2.3) |
| Live Kafka / outbox publish | `OUTBOX_PUBLISHER_ENABLED=false` | `db/outbox.py` Contract-5 envelope construction unit-tested; publish is a documented seam |
| The async Kafka **worker** (`worker/runner.py`) | MVP drives ingest/extract via the authenticated API | documented scale-out seam, not a live consumer in the MVP |
| End-to-end against running compose | not a unit concern | the keyless smoke run (§11) + the platform's Contract-15 smoke tests |

> **Do not break Contract-15 cases 1–10.** cypherx-a1 is a consuming app and must not regress the spine smoke tests; its own suite stays additive and offline so it can run in any CI lane without infrastructure.

---

## 13. Quick reference — test files

| File | Layer | Network-free? | Gate |
| --- | --- | --- | --- |
| `tests/conftest.py` | product harness | yes | sets DB/outbox/revocation off; injects `Principal` |
| `tests/test_api_contracts.py` | product API | yes | health, Contract-2 envelope, `extra="forbid"`, request-id echo |
| `tests/test_github_connector.py` | connector SPI | yes (mock mode) | canonical records, content_sha idempotency, webhook HMAC, parse |
| `tests/test_rls_cross_tenant.py` | DB / RLS | **no** (needs Postgres) | `CYPHERXA1_TEST_DSN` — **mandatory** cross-tenant denial |
| `mcp-eng-memory/tests/conftest.py` | MCP harness | yes | `MANIFEST_PATH`; `FakeBackend`; injects `Principal` |
| `mcp-eng-memory/tests/test_manifest.py` | MCP / Contract 4 | yes | ETag/304, Draft-2020-12 schema conformance, required scopes |
| `mcp-eng-memory/tests/test_invoke.py` | MCP dispatch | yes | routing, input-schema pointer, additionalProperties, scope guard |

Add new tests next to the layer they cover, keep the default suite network-free, and never let a new tenant-scoped table land without a cross-tenant-denial assertion.
