# CypherX — Agent Runner (prototype demo)

A **thin, zero-dependency** single-page UI + backend-for-frontend (BFF) that drives the
live first-cycle spine and **surfaces the per-task step/trace timeline**
(`input guardrail check → LLM call → output guardrail check`, with status / duration /
tokens / cost). It hides the 7-step credential chain behind one `POST /api/run`.

This is a **demo harness**, not the production console (the full Next.js frontend is
deferred, Phase 12). Stdlib Python only — no npm, no pip, no venv.

## Prerequisites
The four services must be running (see the build memory for the run recipe):
`auth :8080`, `xagent :8083`, `llms :8085`, `guardrails :8086`, plus the docker dev stack
(postgres/redpanda/valkey/minio). Verify: `curl localhost:8083/readyz` etc.

## Run
```bash
python "frontend/demo/server.py"
# then open http://localhost:8090
```
On startup the BFF auto-provisions a demo agent (clears the one-time Auth bootstrap
sentinel via `docker exec`, bootstraps a super-admin, creates a worker agent + key,
registers its xAgent runtime config) and caches it in `demo_credentials.json`.

## What you'll see
- **Health strip** — live `readyz` of all four services.
- **Agent card** — the demo agent's model + system prompt + id.
- **Run a task** — free text, or quick chips for the three canonical cases:
  - *Happy path* → `completed`, 3-step timeline, tokens + cost.
  - *Prompt injection* → **blocked** (HTTP 422 `GUARDRAIL_VIOLATION`), input-check step fails.
  - *PII email* → `completed` with the email **redacted** in the response.
- **Timeline** — the ordered audit steps the runtime persists, with per-step duration/tokens,
  plus totals (tokens, cost, duration) and the `trace_id`. Raw JSON is expandable.

## Config (env overrides)
`PORT` (8090), `AUTH_URL`, `XAGENT_URL`, `LLMS_URL`, `GUARDRAILS_URL`, `BOOTSTRAP_TOKEN`,
`PG_CONTAINER`/`PG_USER`/`PG_DB` (for the sentinel reset), `DEMO_SYSTEM_PROMPT`, `DEMO_MODEL`.

## Real LLM (optional)
The timeline shows real tokens/cost even in MOCK mode. To show real model output, restart
the **llms** service with `MOCK_PROVIDERS=false` + `ANTHROPIC_API_KEY=...` (no code change),
then re-run a task here.
