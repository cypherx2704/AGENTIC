#!/usr/bin/env bash
# =====================================================================================================================
# e2e-bootstrap.sh — get a live agent JWT + base URLs for manual end-to-end testing.
#
#   source infra/compose/e2e-bootstrap.sh          # registers a fresh throwaway tenant + mints its orchestrator JWT
#   source infra/compose/e2e-bootstrap.sh you@me.com Passw0rd!23   # log in to an EXISTING account instead
#
# Exports for the rest of your shell: TENANT  AGENT  APIKEY  TOK  AUTHH  +  service base URLs.
# Then every curl in E2E-MANUAL-GUIDE.md is copy-paste. Run from infra/compose/ (any dir works).
# Requires: curl + python on PATH. Note: the stack runs MOCK_PROVIDERS=true, so LLM/embedding
# outputs are deterministic MOCKS (the plumbing is real; the text is canned).
# =====================================================================================================================
export AUTH=http://localhost:8080  LLMS=http://localhost:8085  XAGENT=http://localhost:8083
export GUARD=http://localhost:8086 RAG=http://localhost:8087   MEM=http://localhost:8088
export TOOLS=http://localhost:8089 SKILLS=http://localhost:8095 EDGE=http://localhost:8000
_jget(){ python -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
  echo "logging in existing account $1 ..."
  _r=$(curl -s -X POST "$AUTH/v1/auth/login" -H 'Content-Type: application/json' -d "{\"email\":\"$1\",\"password\":\"$2\"}")
  export TENANT=$(echo "$_r"|_jget tenant_id); export AGENT=$(echo "$_r"|_jget agent_id); export TOK=$(echo "$_r"|_jget token); export APIKEY=""
else
  _email="demo-$(date +%s)-$RANDOM@example.com"
  echo "registering throwaway tenant $_email ..."
  _r=$(curl -s -X POST "$AUTH/v1/auth/register" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$_email\",\"password\":\"Passw0rd!23\",\"tenant_name\":\"Demo Co\"}")
  export TENANT=$(echo "$_r"|_jget tenant_id); export AGENT=$(echo "$_r"|_jget orchestrator_agent_id); export APIKEY=$(echo "$_r"|_jget api_key)
  export TOK=$(curl -s -X POST "$AUTH/v1/agents/$AGENT/token" -H 'Content-Type: application/json' -H "X-Tenant-ID: $TENANT" -d "{\"api_key\":\"$APIKEY\"}"|_jget token)
fi
export AUTHH="Authorization: Bearer $TOK"

if [ -z "$TOK" ]; then echo "!! FAILED — is the stack up? resp: $_r"; else
  echo "  TENANT = $TENANT"
  echo "  AGENT  = $AGENT  (the tenant's orchestrator)"
  echo "  TOK    = <${#TOK}-char JWT>   AUTHH set"
  echo "  scopes : $(python -c "import base64,json;p='$TOK'.split('.')[1];p+='='*(-len(p)%4);print(', '.join(json.loads(base64.urlsafe_b64decode(p)).get('scopes',[])))" 2>/dev/null)"
  echo "  URLs   : AUTH=$AUTH LLMS=$LLMS XAGENT=$XAGENT GUARD=$GUARD RAG=$RAG MEM=$MEM TOOLS=$TOOLS SKILLS=$SKILLS"
fi
