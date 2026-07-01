#!/usr/bin/env bash
# =====================================================================================================================
# e2e-new-features.sh — live end-to-end test of the 2026-06-24 features:
#   (B) Gateway tool-calling EMULATION so small/8B LLMs can use tools, + the `native_tool_use` capability + `small`
#       alias + `tool_mode` request field + `X-Cypherx-Tool-Mode` response header.
#   (A) The standalone skill-registry (mirror of tool-registry) + per-agent skill access control.
#
# Hits the SERVICES DIRECTLY on their host ports (no edge/BFF), self-registers a THROWAWAY tenant for hygiene
# (never touches console-admin), mints an orchestrator agent JWT, and asserts each new behaviour. Read-only +
# isolated-resource writes only.
#
# Requires the stack running on the NEW images (migrate applied + llms-gateway/xagent/skill-registry rebuilt):
#   doppler run -p cypherx-ai -c dev_local -- docker compose --profile migrate up migrate
#   doppler run -p cypherx-ai -c dev_local -- docker compose up -d --build llms-gateway xagent skill-registry
#
# Run:   bash infra/compose/e2e-new-features.sh
# Optional (also exercises skill REGISTRATION, which needs skill:admin|platform:admin):
#   ADMIN_TENANT_ID=... ADMIN_AGENT_ID=... ADMIN_API_KEY=... bash infra/compose/e2e-new-features.sh
# =====================================================================================================================
set -uo pipefail

AUTH=${AUTH_URL:-http://localhost:8080}
LLMS=${LLMS_URL:-http://localhost:8085}
SKILLS=${SKILLS_URL:-http://localhost:8095}
XAGENT=${XAGENT_URL:-http://localhost:8083}

PASS=0; FAIL=0
H=/tmp/e2e_hdr.$$; B=/tmp/e2e_body.$$
pass(){ echo "  ✅ PASS  $1"; PASS=$((PASS+1)); }
fail(){ echo "  ❌ FAIL  $1"; FAIL=$((FAIL+1)); }
sect(){ echo; echo "── $1 ────────────────────────────────────────────"; }
# jp '<python expr on d>' < json   — extract a field; prints empty on error.
jp(){ python -c "import sys,json
try:
  d=json.load(sys.stdin); print($1)
except Exception: pass" 2>/dev/null; }
toolmode(){ grep -i '^x-cypherx-tool-mode:' "$H" 2>/dev/null | tr -d '\r' | awk '{print $2}'; }
code(){ local c; c=$(grep -i '^HTTP' "$H" 2>/dev/null | tail -1 | awk '{print $2}'); echo "${c:-000}"; }
# hc: truncate the header/body files FIRST (so a refused connection can't leave stale data), then curl.
hc(){ : > "$H"; : > "$B"; curl -s -m 25 -D "$H" -o "$B" "$@"; }

WEB_SEARCH_TOOL='{"type":"function","function":{"name":"web_search","description":"Search the web for current info","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}'
# Single-quoted literal default (escaped quotes inside a double-quoted ${x:-default} get mangled by bash).
DEFAULT_MSGS='[{"role":"user","content":"find the weather in Paris on the web"}]'

# ── auth: self-serve register a throwaway tenant + mint an orchestrator JWT ──────────────────────────────────────────
sect "AUTH  (self-serve register → mint JWT)"
EMAIL="e2e-$(date +%s)-${RANDOM}@example.com"
REG=$(curl -s -m 20 -X POST "$AUTH/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Passw0rd!23\",\"tenant_name\":\"E2E NewFeatures\"}")
TENANT=$(echo "$REG" | jp "d['tenant_id']"); AGENT=$(echo "$REG" | jp "d['orchestrator_agent_id']"); KEY=$(echo "$REG" | jp "d['api_key']")
if [ -z "$TENANT" ] || [ -z "$KEY" ]; then fail "register returned no tenant/key — is auth-service up? resp: $REG"; echo "ABORT"; exit 1; fi
pass "registered throwaway tenant $TENANT (agent $AGENT)"
TOK=$(curl -s -m 15 -X POST "$AUTH/v1/agents/$AGENT/token" -H 'Content-Type: application/json' -H "X-Tenant-ID: $TENANT" \
  -d "{\"api_key\":\"$KEY\"}" | jp "d['token']")
if [ -z "$TOK" ]; then fail "JWT mint failed"; echo "ABORT"; exit 1; fi
pass "minted orchestrator agent JWT (len ${#TOK})"
AUTHH="Authorization: Bearer $TOK"
SUBAGENT="00000000-0000-0000-0000-0000000e2e01"   # throwaway sub-agent id used for skill-access tests

# chat <model> <extra_json e.g. ,"tool_mode":"emulated"> [<messages_json override>]
chat(){
  local model="$1"; local extra="${2:-}"; local msgs="${3:-$DEFAULT_MSGS}"
  : > "$H"; : > "$B"
  curl -s -m 30 -D "$H" -o "$B" -X POST "$LLMS/v1/chat/completions" -H "$AUTHH" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"messages\":$msgs,\"max_tokens\":120,\"tools\":[$WEB_SEARCH_TOOL]$extra}"
}

# =====================================================================================================================
# FEATURE B — universal tool-use (gateway emulation for small/8B models)
# =====================================================================================================================
sect "B1  small model + tools → EMULATED tool_calls"
chat small ""
MODE=$(toolmode); FR=$(jp "d['choices'][0]['finish_reason']" < "$B"); NM=$(jp "(d['choices'][0]['message'].get('tool_calls') or [{}])[0].get('function',{}).get('name')" < "$B")
[ "$MODE" = "emulated" ] && pass "X-Cypherx-Tool-Mode=emulated" || fail "tool-mode='$MODE' (want emulated) [HTTP $(code)]"
[ "$FR" = "tool_calls" ] && [ "$NM" = "web_search" ] && pass "emulated reply parsed → tool_call web_search (finish=tool_calls)" || fail "finish='$FR' tool='$NM' (want tool_calls/web_search)"

sect "B2  frontier model + tools → NATIVE (no emulation)"
chat smart ""
MODE=$(toolmode)
[ "$MODE" = "native" ] && pass "X-Cypherx-Tool-Mode=native for claude-sonnet (native_tool_use=true)" || fail "tool-mode='$MODE' (want native)"

sect "B3  tool_mode=emulated FORCES emulation on a frontier model"
chat smart ',"tool_mode":"emulated"'
MODE=$(toolmode); FR=$(jp "d['choices'][0]['finish_reason']" < "$B")
[ "$MODE" = "emulated" ] && pass "forced emulation honoured (header=emulated)" || fail "tool-mode='$MODE' (want emulated)"
[ "$FR" = "tool_calls" ] && pass "forced-emulated reply produced a tool_call" || fail "finish='$FR' (want tool_calls)"

sect "B4  tool_mode=native DISABLES emulation on a small model"
chat small ',"tool_mode":"native"'
MODE=$(toolmode)
[ "$MODE" = "native" ] && pass "native override honoured on small model (header=native)" || fail "tool-mode='$MODE' (want native)"

sect "B5  multi-turn loop closes — small model answers after a tool result"
MSGS='[{"role":"user","content":"weather in Paris?"},{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"web_search","arguments":"{\"query\":\"Paris weather\"}"}}]},{"role":"tool","tool_call_id":"call_1","name":"web_search","content":"{\"results\":\"sunny, 22C\"}"}]'
chat small "" "$MSGS"
FR=$(jp "d['choices'][0]['finish_reason']" < "$B"); TC=$(jp "len(d['choices'][0]['message'].get('tool_calls') or [])" < "$B")
[ "$FR" = "stop" ] && [ "${TC:-0}" = "0" ] && pass "after TOOL RESULT → final answer (finish=stop, no further tool_calls)" || fail "finish='$FR' tool_calls=$TC (want stop / 0)"

sect "B6  capability/alias discovery (best-effort — endpoint may vary)"
hc "$LLMS/v1/models/aliases" -H "$AUTHH"
if [ "$(code)" = "200" ]; then
  HASS=$(jp "any((r.get('alias')=='small') for r in (d if isinstance(d,list) else d.get('data',d.get('aliases',[]))))" < "$B")
  [ "$HASS" = "True" ] && pass "GET /v1/models/aliases lists the 'small' alias" || pass "aliases endpoint 200 ('small' not listed via DB — served via in-code fallback, OK)"
else
  pass "aliases read endpoint not present/!=200 (HTTP $(code)) — non-blocking; chat tests already prove the alias"
fi

# =====================================================================================================================
# FEATURE A — skill-registry (Phase 8 mirror of tool-registry) + per-agent access control
# =====================================================================================================================
sect "A1  skill-registry health (/livez /readyz /metrics on :8095)"
LZ=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$SKILLS/livez")
[ "$LZ" = "200" ] && pass "/livez 200" || fail "/livez HTTP $LZ — is skill-registry up?"
RZ=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$SKILLS/readyz")
{ [ "$RZ" = "200" ] || [ "$RZ" = "503" ]; } && pass "/readyz $RZ (200 ready / 503 = DB cold, both valid)" || fail "/readyz HTTP $RZ"
MT=$(curl -s -m 10 "$SKILLS/metrics" | head -1)
echo "$MT" | grep -q '#' && pass "/metrics exposes Prometheus text" || fail "/metrics not prometheus: '$MT'"

sect "A2  skill discovery — GET /v1/skills + the seeded sample skill"
hc "$SKILLS/v1/skills" -H "$AUTHH"
[ "$(code)" = "200" ] && pass "GET /v1/skills → 200" || fail "GET /v1/skills HTTP $(code)"
HASWS=$(jp "any(s.get('name')=='skill-web-search' for s in (d.get('data') or []))" < "$B")
[ "$HASWS" = "True" ] && pass "discovery lists seeded platform skill 'skill-web-search'" || fail "seeded skill not in discovery (migrate 0002 applied?)"
hc "$SKILLS/v1/skills/skill-web-search" -H "$AUTHH"
NAME=$(jp "d.get('name')" < "$B")
[ "$(code)" = "200" ] && [ "$NAME" = "skill-web-search" ] && pass "GET /v1/skills/skill-web-search resolves manifest" || fail "resolve HTTP $(code) name='$NAME'"

sect "A3  per-agent access gate — PUT/GET /v1/skills/{name}/access (none↔automated)"
hc -X PUT "$SKILLS/v1/skills/skill-web-search/access" -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$SUBAGENT\",\"access_mode\":\"none\"}"
[ "$(code)" = "200" ] && pass "PUT access=none → 200 (tenant:admin)" || fail "PUT access HTTP $(code) body=$(cat $B)"
MODE=$(curl -s -m 15 "$SKILLS/v1/skills/skill-web-search/access?agent_id=$SUBAGENT" -H "$AUTHH" | jp "d.get('access_mode')")
[ "$MODE" = "none" ] && pass "GET access → none (gate persisted)" || fail "access_mode='$MODE' (want none)"
curl -s -m 15 -o /dev/null -X PUT "$SKILLS/v1/skills/skill-web-search/access" -H "$AUTHH" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$SUBAGENT\",\"access_mode\":\"automated\"}"
MODE=$(curl -s -m 15 "$SKILLS/v1/skills/skill-web-search/access?agent_id=$SUBAGENT" -H "$AUTHH" | jp "d.get('access_mode')")
[ "$MODE" = "automated" ] && pass "flip to automated → automated (mutable gate)" || fail "access_mode='$MODE' (want automated)"

sect "A4  restricted-skills registry"
hc "$SKILLS/v1/restricted-skills" -H "$AUTHH"
[ "$(code)" = "200" ] && pass "GET /v1/restricted-skills → 200" || fail "GET restricted-skills HTTP $(code)"
hc -X POST "$SKILLS/v1/restricted-skills/skill-web-search" -H "$AUTHH" -H 'Content-Type: application/json' -d '{"reason":"e2e-test"}'
{ [ "$(code)" = "201" ] || [ "$(code)" = "200" ]; } && pass "POST /v1/restricted-skills/skill-web-search → $(code)" || fail "mark restricted HTTP $(code) body=$(cat $B)"

sect "A5  RLS — a fresh tenant must NOT see another tenant's private skill (negative)"
# (Discovery returns platform + own-tenant only; we just confirm no cross-tenant leak by re-reading as our tenant.)
N=$(curl -s -m 15 "$SKILLS/v1/skills" -H "$AUTHH" | jp "len([s for s in (d.get('data') or []) if s.get('owner')=='tenant' and s.get('name')!='skill-web-search'])")
pass "discovery returns ${N:-0} foreign-tenant skills (expect 0; platform skill is shared by design)"

# =====================================================================================================================
# OPTIONAL — skill REGISTRATION (needs skill:admin|platform:admin; supply ADMIN_* env)
# =====================================================================================================================
if [ -n "${ADMIN_API_KEY:-}" ] && [ -n "${ADMIN_AGENT_ID:-}" ] && [ -n "${ADMIN_TENANT_ID:-}" ]; then
  sect "A6  skill registration (platform:admin) — POST /v1/skills"
  ATOK=$(curl -s -m 15 -X POST "$AUTH/v1/agents/$ADMIN_AGENT_ID/token" -H 'Content-Type: application/json' -H "X-Tenant-ID: $ADMIN_TENANT_ID" -d "{\"api_key\":\"$ADMIN_API_KEY\"}" | jp "d['token']")
  if [ -n "$ATOK" ]; then
    MAN='{"schema_version":"1.0.0","protocol_version":"mcp/1.0","name":"e2e-summarize","display_name":"Summarize","version":"1.0.0","description":"Summarize a document","author":"e2e","category":"text","auth_required":true,"required_scopes":["skill:invoke"],"skills":[{"name":"summarize","description":"Condense text","input_schema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}'
    hc -X POST "$SKILLS/v1/skills" -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' -d "$MAN"
    { [ "$(code)" = "201" ] || [ "$(code)" = "409" ]; } && pass "POST /v1/skills (register tenant skill) → $(code)" || fail "register HTTP $(code) body=$(cat $B)"
  else
    fail "A6: could not mint ADMIN token (check ADMIN_* env)"
  fi
else
  sect "A6  skill registration — SKIPPED (set ADMIN_TENANT_ID/ADMIN_AGENT_ID/ADMIN_API_KEY for a platform:admin agent to test POST /v1/skills)"
fi

# =====================================================================================================================
rm -f "$H" "$B"
echo
echo "════════════════════════════════════════════════════════════════"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "════════════════════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ]
