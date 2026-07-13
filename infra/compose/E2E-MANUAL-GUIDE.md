# CypherX — End-to-End Manual Test Guide

A hands-on walkthrough of the **entire platform**, feature by feature, against your **running local
stack**. Every route/payload here was pulled from the live services, so commands are copy-paste.

There are two ways to drive the platform — do **both**:
- **Part 1 — The Console (browser):** the real user journey, clicking through the admin UI.
- **Parts 2+ — The API (curl):** every feature, including ones the UI doesn't surface (tool emulation,
  skills, raw audit trail).

> **Mock mode.** The stack runs `MOCK_PROVIDERS=true` + `MOCK_EMBEDDINGS=true`. LLM replies and
> embeddings are **deterministic mocks** — the *plumbing* (routing, governance, pipeline, metering,
> guardrails, tools) is fully real; only the model text/vectors are canned. To use a real model,
> register a BYOK connection (`POST /v1/keys`) and the gateway uses it instead.

> **Shells.** The curl blocks assume **Git Bash** (`bash`). PowerShell users: run `bash <script>`.
> Requires `curl` + `python` on PATH.

---

## Port / URL map

| What | URL |
|---|---|
| Console (edge → SPA + BFF) | http://localhost:8000 |
| SPA direct | http://localhost:3000 |
| auth | http://localhost:8080 |
| xagent | http://localhost:8083 |
| llms-gateway | http://localhost:8085 |
| guardrails | http://localhost:8086 |
| rag | http://localhost:8087 |
| memory | http://localhost:8088 |
| tool-registry | http://localhost:8089 |
| skill-registry | http://localhost:8095 |
| mailpit (catches verification emails) | http://localhost:8025 |
| redpanda console / Kafka | broker `localhost:9092` |

Quick health of everything: `docker compose ps` (from `infra/compose/`).

---

# PART 1 — The Console (browser, the real user journey)

1. **Open** http://localhost:8000 → you're redirected to **/login**.
2. **Register** → click *Create an account* → email + password (+ workspace name). On submit the platform
   provisions **tenant → user → orchestrator agent → an initial api_key (shown ONCE — copy it)**. You land
   logged in. (Password fields have the eye-icon toggle.)
   - *Google sign-in* is wired (the "Continue with Google" button); it needs the Google OAuth consent
     screen to allow your account — otherwise email/password is the path.
3. **Dashboard** (`/`) — overview.
4. **/orchestrator** — your tenant's mandatory orchestrator + its config; create/manage **sub-agents** here.
5. **/agents**, **/agents/{id}** — all agents; tag shows orchestrator / sub-agent / user-created.
6. **/llms** + **/llms/aliases** — model aliases (fast/smart/small/…), per-agent allowlists, user LLM rules.
7. **/keys** — BYOK "LLM Connections" (register a real provider key to leave mock mode).
8. **/guardrails** — input/output policies + rules + violations.
9. **/rag** — knowledge bases (create, upload docs, query).
10. **/tools** — tool catalogue + per-agent access mode (none / ask / automated).
11. **/tasks** + **/tasks/run** — **submit a task to an agent and watch it execute** (live step timeline);
    **/tasks/{id}** shows the full audit trail.
12. **/hil** — human-in-the-loop approval queue (grant/deny pending tool/skill/sub-agent actions).
13. **/audit**, **/usage**, **/tenant**, **/health** — audit log, token/cost usage, tenant settings, health.

> The single best "is it all working?" move in the UI: **/tasks/run** → pick the orchestrator → send a
> message → watch the step timeline (LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT).

---

# PART 2 — API setup (get a live token)

```bash
cd infra/compose
source e2e-bootstrap.sh          # registers a throwaway tenant + mints its orchestrator JWT
# (or, to use an existing account:  source e2e-bootstrap.sh you@example.com YourPassword )
```
This exports `TENANT AGENT APIKEY TOK AUTHH` + all service URLs and prints your token's scopes
(orchestrator defaults: `tenant:admin tenant:read orchestrator:manage hil:approve agent:* llm:invoke
guardrails:check rag:* mem:*`). Handy pretty-printer: append `| python -m json.tool`.

---

# PART 3 — Auth & onboarding

```bash
# Register (what the bootstrap did) — returns the orchestrator's one-time api_key:
curl -s -X POST $AUTH/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"alice@acme.com","password":"Passw0rd!23","tenant_name":"Acme"}' | python -m json.tool

# Mint an agent JWT from an api_key (note the REQUIRED X-Tenant-ID header):
curl -s -X POST $AUTH/v1/agents/$AGENT/token -H 'Content-Type: application/json' \
  -H "X-Tenant-ID: $TENANT" -d "{\"api_key\":\"$APIKEY\"}" | python -m json.tool

# Who am I / my tenant:
curl -s $AUTH/v1/tenants/me -H "$AUTHH" | python -m json.tool
# Decode your JWT to see agent_type=orchestrator + scopes:
echo "$TOK" | python -c "import sys,base64,json;p=sys.stdin.read().split('.')[1];p+='='*(-len(p)%4);print(json.dumps(json.loads(base64.urlsafe_b64decode(p)),indent=2))"
```
**Expect:** register → `{tenant_id, orchestrator_agent_id, api_key, key_prefix}`; token → `{token, scopes,
expires_in}`; the decoded JWT has `"agent_type":"orchestrator"`.

---

# PART 4 — Orchestrator & sub-agents (RBAC)

```bash
# Create a sub-agent. Its scopes MUST be a subset of the orchestrator's (else 422).
curl -s -X POST $AUTH/v1/orchestrator/sub-agents -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"name":"research-bot","allowed_scopes":["llm:invoke","agent:execute"]}' | python -m json.tool

# List sub-agents owned by this orchestrator:
curl -s $AUTH/v1/orchestrator/sub-agents -H "$AUTHH" | python -m json.tool

# RBAC guard — request a scope the orchestrator does NOT hold → 422 (scope inheritance enforced):
curl -s -o /dev/null -w 'scope-escalation -> HTTP %{http_code}\n' -X POST $AUTH/v1/orchestrator/sub-agents \
  -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"name":"bad","allowed_scopes":["platform:admin"]}'
```
**Expect:** create → `201` with the sub-agent (`agent_type:sub_agent`, `parent_orchestrator_id` = your
orchestrator). Scope-escalation → `422`. (Depth guard: a sub-agent's own JWT calling this endpoint → `403
SUB_AGENT_CANNOT_DELEGATE`.)

---

# PART 5 — LLM gateway & governance

```bash
# Aliases + model catalog:
curl -s $LLMS/v1/models/aliases -H "$AUTHH" | python -m json.tool   # fast/smart/small/default/code/vision/embed...
curl -s $LLMS/v1/models -H "$AUTHH" | python -m json.tool

# Plain chat (mock reply):
curl -s -X POST $LLMS/v1/chat/completions -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"model":"smart","messages":[{"role":"user","content":"hello"}],"max_tokens":50}' | python -m json.tool
```

### ⭐ Small-LLM tool-use (gateway emulation — the headline feature)
```bash
TOOLS_JSON='[{"type":"function","function":{"name":"web_search","description":"Search the web","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}]'

# 1) A SMALL model (native_tool_use=false) → gateway EMULATES tool-calling. Watch the response HEADER:
curl -s -D - -X POST $LLMS/v1/chat/completions -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"model\":\"small\",\"messages\":[{\"role\":\"user\",\"content\":\"find the weather in Paris\"}],\"max_tokens\":120,\"tools\":$TOOLS_JSON}" \
  | grep -iE '^x-cypherx-tool-mode|"tool_calls"|"finish_reason"'
#   => x-cypherx-tool-mode: emulated   + message.tool_calls=[web_search]  + finish_reason=tool_calls

# 2) A FRONTIER model → native (no emulation):
curl -s -D - -o /dev/null -X POST $LLMS/v1/chat/completions -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"model\":\"smart\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":50,\"tools\":$TOOLS_JSON}" \
  | grep -i '^x-cypherx-tool-mode'      # => native

# 3) Force emulation on ANY model with tool_mode:
curl -s -D - -o /dev/null -X POST $LLMS/v1/chat/completions -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"model\":\"smart\",\"tool_mode\":\"emulated\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":80,\"tools\":$TOOLS_JSON}" \
  | grep -i '^x-cypherx-tool-mode'      # => emulated
```

### Per-agent LLM allowlist (tenant:admin)
```bash
# Restrict this agent to ONLY 'smart', then prove 'fast' is rejected:
curl -s -X PUT $LLMS/v1/agents/$AGENT/llm-aliases -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"aliases":["smart"]}' | python -m json.tool
curl -s -o /dev/null -w 'fast-while-allowlisted -> HTTP %{http_code}\n' -X POST $LLMS/v1/chat/completions \
  -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'   # => 403 LLM_ALIAS_NOT_ALLOWED
curl -s -X PUT $LLMS/v1/agents/$AGENT/llm-aliases -H "$AUTHH" -H 'Content-Type: application/json' -d '{"aliases":[]}' >/dev/null  # reset (empty = unrestricted)
```

### User-defined LLM rules (tenant:admin)
```bash
# Block a specific model for the whole tenant:
curl -s -X POST $LLMS/v1/llm-rules -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"provider":"anthropic","model_id":"claude-opus-4-8","rule_type":"block"}' | python -m json.tool
curl -s -o /dev/null -w 'blocked-model -> HTTP %{http_code}\n' -X POST $LLMS/v1/chat/completions \
  -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'   # => 403 LLM_RULE_BLOCKED
curl -s $LLMS/v1/llm-rules -H "$AUTHH" | python -m json.tool      # list rules (grab rule_id to delete)
# Billing-bypass rule: add one with "billing_bypass":true → that model's calls return header X-Cypherx-Billing-Bypassed: true and write NO usage row.
```

---

# PART 6 — Agent runtime config + task execution (the pipeline)

xAgent has its own runtime config per agent. Configure it, then submit tasks.

```bash
# Configure the orchestrator's runtime (name + system_prompt are required):
curl -s -X PUT $XAGENT/v1/agents/$AGENT/runtime -H "$AUTHH" -H 'Content-Type: application/json' -d '{
  "name":"orchestrator","system_prompt":"You are a concise, helpful assistant.",
  "llm_model":"smart","max_tokens":256,"temperature":0.3,
  "memory_scope":"none","allowed_tools":[],"allowed_skills":[],"allowed_kb_ids":[]
}' | python -m json.tool

# Submit a task (sync) — runs LOAD→PRE_GUARDRAIL→PROMPT_BUILD→LLM→POST_GUARDRAIL→EVENT:
RESP=$(curl -s -X POST $XAGENT/v1/tasks -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT\",\"input\":{\"message\":\"Summarize what CypherX does in one line.\"}}")
echo "$RESP" | python -m json.tool
TASK=$(echo "$RESP" | python -c "import sys,json;print(json.load(sys.stdin).get('task_id',''))")

# Inspect the task + its AUDIT TRAIL. The step trail is under the "task_steps" key,
# each entry: {step, status, duration_ms}.
curl -s $XAGENT/v1/tasks/$TASK -H "$AUTHH" | python -m json.tool
curl -s $XAGENT/v1/tasks/$TASK -H "$AUTHH" | python -c "import sys,json;[print(' ',s['step'],s['status'],str(s.get('duration_ms'))+'ms') for s in json.load(sys.stdin).get('task_steps',[])]"
```
**Expect:** a Contract-3 A2A response with the (mock) answer + `status:completed`; `task_steps` has
`guardrail_check_input`, `llm_call`, `guardrail_check_output` (all `passed`).

### Async + live SSE stream
```bash
# Async needs an Idempotency-Key:
A=$(curl -s -X POST "$XAGENT/v1/tasks" -H "$AUTHH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(date +%s)-$RANDOM" \
  -d "{\"agent_id\":\"$AGENT\",\"mode\":\"async\",\"input\":{\"message\":\"hello async\"}}")
echo "$A" | python -m json.tool                 # => 202 + task_id
AID=$(echo "$A"|python -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
curl -s -N $XAGENT/v1/tasks/$AID/stream -H "$AUTHH"     # Server-Sent Events: stage/step progress + terminal result
```

---

# PART 7 — Guardrails (input/output safety)

Guardrails run automatically as the PRE/POST stages of every task. With `CLASSIFIER_MODE=stub` the default
verdict is **allow**, so to *see* enforcement you add a blocking rule and assign it.

```bash
# See guardrail steps on the task you already ran (status passed):
curl -s $XAGENT/v1/tasks/$TASK -H "$AUTHH" | python -c "import sys,json;[print(s['step'],s['status']) for s in json.load(sys.stdin).get('task_steps',[])]"

# Inspect policies / rules / recent violations (tenant:admin):
curl -s $GUARD/v1/policies   -H "$AUTHH" | python -m json.tool
curl -s $GUARD/v1/rules      -H "$AUTHH" | python -m json.tool
curl -s $GUARD/v1/violations -H "$AUTHH" | python -m json.tool
# To trip a block: POST /v1/rules a keyword/regex rule, POST /v1/policies/{id}/assign it to the agent,
# then submit a task containing that keyword → the task fails with GUARDRAIL_VIOLATION (a 'failed'
# guardrail_check step). /v1/policies/simulate lets you dry-run a policy against sample text first.
```

---

# PART 8 — Tools, per-agent access modes & HIL

```bash
# Discover tools (platform + your tenant). The public web_search flow-tool (server `mcp-web-search`,
# which replaced the retired tool-web-search service) is discoverable to every tenant:
curl -s $TOOLS/v1/tools -H "$AUTHH" | python -m json.tool        # => mcp-web-search
curl -s $TOOLS/v1/tools/mcp-web-search -H "$AUTHH" | python -m json.tool

# Per-agent access mode (none|ask|automated) — tenant:admin. Flip it and read it back:
curl -s -X PUT $TOOLS/v1/tools/mcp-web-search/access -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT\",\"access_mode\":\"automated\"}" | python -m json.tool
curl -s "$TOOLS/v1/tools/mcp-web-search/access?agent_id=$AGENT" -H "$AUTHH" | python -m json.tool

# Restricted-tools registry:
curl -s $TOOLS/v1/restricted-tools -H "$AUTHH" | python -m json.tool
curl -s -X POST $TOOLS/v1/restricted-tools/mcp-web-search -H "$AUTHH" -H 'Content-Type: application/json' -d '{"reason":"demo"}' | python -m json.tool
```

### Drive a tool through a task (the tool loop)
The tool loop is enabled in this stack (`STAGE_ENABLE_TOOL_LOOP=true`). It triggers only when the agent's
runtime lists `allowed_tools`. Reconfigure the agent and submit a tool-y task:
```bash
curl -s -X PUT $XAGENT/v1/agents/$AGENT/runtime -H "$AUTHH" -H 'Content-Type: application/json' -d '{
  "name":"orchestrator","system_prompt":"Use tools when helpful.","llm_model":"small",
  "allowed_tools":["mcp-web-search"],"allowed_skills":[],"allowed_kb_ids":[],"memory_scope":"none"}' >/dev/null
R=$(curl -s -X POST $XAGENT/v1/tasks -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT\",\"input\":{\"message\":\"search the web for today's weather in Paris\"}}")
echo "$R" | python -c "import sys,json;d=json.load(sys.stdin);print('status:',d.get('status'));[print(' step:',s['step'],s['status']) for s in d.get('task_steps',[])]"
```
**What you'll see:** a `tool_call` step. Because `llm_model:small`, the gateway **emulates** the tool call,
so an 8B model invokes the tool just like a frontier one. With `access_mode:none` the step is
`tool_access_denied`; with `ask` it pauses for HIL (Part 12).

> A *successful* tool invocation also needs the agent JWT to carry the tool's invoke scope
> (`tool:mcp-web-search:invoke`) — orchestrators don't have it by default, so the invoke is fed back to the
> model as a scope error (fail-soft). Grant the scope on the api_key **and** agent to see a clean invoke.

---

# PART 9 — Skills (Phase 8 registry + access)

```bash
curl -s $SKILLS/v1/skills -H "$AUTHH" | python -m json.tool                 # => skill-web-search (seeded)
curl -s $SKILLS/v1/skills/skill-web-search -H "$AUTHH" | python -m json.tool
# Per-agent skill access gate (none|ask|automated) — tenant:admin (mirror of tools):
curl -s -X PUT $SKILLS/v1/skills/skill-web-search/access -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT\",\"access_mode\":\"automated\"}" | python -m json.tool
curl -s "$SKILLS/v1/skills/skill-web-search/access?agent_id=$AGENT" -H "$AUTHH" | python -m json.tool
```
- **Registering a skill** (`POST /v1/skills`) needs `skill:admin` (like `tool:admin`) — not in the
  orchestrator default scopes, so grant it on the api_key **and** agent first (see Part 14).
- **SKILL_LOAD stage** is off by default (`STAGE_ENABLE_SKILL_LOAD`). To exercise it: set that env on xagent,
  give the agent `allowed_skills:["skill-web-search"]`, and submit a task — the permitted skills get spliced
  into the prompt (visible as a `skill_load` step).

---

# PART 10 — RAG (knowledge bases)

```bash
# Create a KB, add a document inline, then query it:
KB=$(curl -s -X POST $RAG/v1/kbs -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"name":"handbook","description":"company handbook"}' | python -c "import sys,json;print(json.load(sys.stdin)['kb_id'])")
echo "kb=$KB"
curl -s -X POST $RAG/v1/kbs/$KB/documents -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"title":"pto","content":"Employees get 25 days of paid time off per year."}' | python -m json.tool
curl -s $RAG/v1/kbs/$KB/status -H "$AUTHH" | python -m json.tool     # ingestion/chunking status
curl -s -X POST $RAG/v1/kbs/$KB/query -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"query":"how many PTO days?","top_k":3}' | python -m json.tool
```
Then make an agent RAG-aware: `PUT .../runtime` with `"allowed_kb_ids":["'$KB'"]` and submit a task — the
`RAG_QUERY` stage retrieves chunks and the `PROMPT_BUILD` stage splices them in.
> Retrieval scores are **mock** here (`MOCK_EMBEDDINGS=true`) — the wiring is real, the ranking is synthetic.

---

# PART 11 — Memory

```bash
curl -s -X POST $MEM/v1/memories -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"content":"The user prefers metric units.","type":"note","scope":"principal_only"}' | python -m json.tool
curl -s -X POST $MEM/v1/memories/search -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"query":"units preference","top_k":5}' | python -m json.tool
```
Agent integration: `PUT .../runtime` with `"memory_scope":"agent"` → tasks run `MEMORY_RETRIEVE` (into the
prompt) and `MEMORY_WRITE` (after success). GDPR wipe: `POST $MEM/v1/gdpr/wipe`.

---

# PART 12 — Human-in-the-loop (HIL)

```bash
# Orchestrator HIL mode (automated | human_in_loop | partial):
curl -s $AUTH/v1/orchestrator/hil-config -H "$AUTHH" | python -m json.tool
curl -s -X PUT $AUTH/v1/orchestrator/hil-config -H "$AUTHH" -H 'Content-Type: application/json' \
  -d '{"default_mode":"human_in_loop","ask_on_triggers":[]}' | python -m json.tool

# Pending approval queue + grant/deny (also the /hil page in the Console):
curl -s $AUTH/v1/hil/approvals -H "$AUTHH" | python -m json.tool
# curl -s -X POST $AUTH/v1/hil/approvals/<REQUEST_ID>/grant -H "$AUTHH"
```
**Flow:** set a tool to `access_mode:ask`, set HIL mode to `human_in_loop`, then submit a task that calls
that tool → the task **pauses**, a request appears in `/v1/hil/approvals` (and the Console `/hil` page);
**grant** it → the task resumes; **deny**/timeout → the tool call is refused.

---

# PART 13 — Observability (events, audit, usage, metrics, traces)

```bash
# Token/cost usage + metering (gateway + auth):
curl -s "$LLMS/v1/usage" -H "$AUTHH" | python -m json.tool
curl -s "$LLMS/v1/cost"  -H "$AUTHH" | python -m json.tool
curl -s "$AUTH/v1/usage" -H "$AUTHH" | python -m json.tool

# Prometheus metrics (no auth):
curl -s $XAGENT/metrics | grep -E '^xagent_|^stage_' | head
curl -s $LLMS/metrics   | grep -E '^llms_|tokens' | head

# Kafka events (Contract-5 envelopes) on Redpanda:
docker exec cypherx-redpanda rpk topic list
docker exec cypherx-redpanda rpk topic consume cypherx.agent.task.completed --num 1   # Ctrl-C to stop
```
- **Audit log:** Console `/audit` page, or `GET $AUTH/v1/audit/export`. Every auth/agent/task action is recorded.
- **Traces:** opt-in — bring the stack up with `--profile observability` and set
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317`, then view traces in Grafana/Tempo (`:3001`).

---

# PART 14 — Appendix: granting a NEW scope (for `skill:admin` / `tool:invoke` tests)

Token scopes = **api_key.scopes ∩ agent.allowed_scopes** (intersection at mint). To grant a scope an
orchestrator lacks by default (e.g. `skill:admin`, `tool:mcp-web-search:invoke`), add it to **both** the
api_key and the agent, then re-mint. Quick DB way (Doppler is blocked locally, so use the owner DSN from
`.env`):
```bash
cd infra/compose
PGURL=$(grep '^MIGRATE_DATABASE_URL=' .env | sed 's/^MIGRATE_DATABASE_URL=//'); export PGURL
SCOPE='skill:admin'; export SCOPE AID="$AGENT" TID="$TENANT"
docker run --rm -e PGURL -e AID -e TID -e SCOPE postgres:16-alpine sh -c \
 'psql "$PGURL" -q -c "SELECT set_config('"'"'app.tenant_id'"'"','"'"''"$TID"''"'"',false);" \
   -c "UPDATE auth.agents   SET allowed_scopes=allowed_scopes||ARRAY['"'"''"$SCOPE"''"'"'] WHERE agent_id='"'"''"$AID"''"'"' AND NOT('"'"''"$SCOPE"''"'"'=ANY(allowed_scopes));" \
   -c "UPDATE auth.api_keys SET scopes=scopes||ARRAY['"'"''"$SCOPE"''"'"'] WHERE agent_id='"'"''"$AID"''"'"' AND NOT('"'"''"$SCOPE"''"'"'=ANY(scopes));"'
# then re-source to re-mint with the new scope:  source e2e-bootstrap.sh  (will register a NEW tenant —
# instead re-mint THIS agent: curl POST $AUTH/v1/agents/$AGENT/token -H "X-Tenant-ID:$TENANT" -d {api_key:$APIKEY})
```

---

## Notes
- Each `source e2e-bootstrap.sh` creates a **fresh throwaway tenant** (isolated; safe). Don't point destructive
  tests at the Console-admin tenant.
- Reset the whole platform fresh: `docker compose down && docker compose --profile migrate up migrate && docker compose up -d`.
- If a service looks wedged: `docker compose logs --tail=50 <service>` and `docker compose ps`.
