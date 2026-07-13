#!/bin/sh
# =====================================================================================================================
# infra/compose/migrate.sh — one-shot CypherX schema bootstrap against Neon (DIRECT endpoint).
#
# Applied, in this order, against $MIGRATE_DATABASE_URL using psql (from the postgres:16-alpine image):
#   0. CREATE EXTENSION IF NOT EXISTS pgcrypto / vector  (idempotent; pgcrypto = gen_random_uuid, vector = pgvector)
#   1. for each service in [auth, llms, guardrails, xagent, rag, memory, tool-registry, skill-registry]:
#        a. every db/migrations/*__init.sql  (sorted; create schema + runtime role + tables + RLS — idempotent)
#        b. every db/migrations/*__seed.sql  (sorted; platform-default rows etc. — idempotent)
#   2. PROVISION RUNTIME ROLES: set each *_user role's PASSWORD + per-role search_path from env (idempotent ALTER
#      ROLE). The init scripts CREATE the roles LOGIN but WITHOUT a password; Neon needs a password to authenticate
#      and the apps connect on the POOLED endpoint as these non-owner roles so RLS stays enforced. The *_DB_PASSWORD
#      env vars are OPTIONAL — a role whose password var is empty is skipped (left as-is), so a partial re-run is safe.
#
# WHY THE DIRECT ENDPOINT: the init scripts take session-level advisory locks / do DDL that the Neon POOLED
# (transaction-mode) endpoint cannot hold across statements. Run this with MIGRATE_DATABASE_URL pointed at the
# Neon DIRECT (non-pooler) host, as the OWNER/admin role. sslmode=require is mandatory for Neon.
#
# Each service's migrations live under /migrations/<service>/ (mounted read-only by docker-compose.yml).
# The *__init.sql files are idempotent (CREATE ... IF NOT EXISTS, DO $$ ... $$ role guards), so re-running is safe.
#
# Invoked by:  docker compose --profile migrate up migrate
# =====================================================================================================================
set -eu

if [ -z "${MIGRATE_DATABASE_URL:-}" ]; then
  echo "FATAL: MIGRATE_DATABASE_URL is not set. Point it at the Neon DIRECT endpoint (owner role)." >&2
  exit 1
fi

# psql flags: stop on first error, treat errors in scripts as fatal, no client messages noise.
PSQL="psql --set=ON_ERROR_STOP=1 --no-psqlrc --quiet"

# Run a single SQL file, echoing the step.
run_file() {
  f="$1"
  echo "  -> applying $f"
  $PSQL "$MIGRATE_DATABASE_URL" -f "$f"
}

echo "==================================================================================="
echo "CypherX migrate: applying schemas/roles/RLS/seed against the Neon DIRECT endpoint."
echo "==================================================================================="

# --- Step 0: extensions (idempotent). pgcrypto is required by every service; vector for memory/rag embeddings. ------
echo "[0/10] CREATE EXTENSION pgcrypto, vector"
$PSQL "$MIGRATE_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
# vector (pgvector) may not be pre-enabled on every Neon project; create it best-effort so migrate does not hard-fail
# on a project where it is unavailable. (No current migration uses it, but the goal mandates the extension step.)
if $PSQL "$MIGRATE_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null; then
  echo "      vector extension ready"
else
  echo "      WARNING: could not create 'vector' extension (not available on this Neon project) — continuing." >&2
fi

# --- Steps 1..7: per-service init then seed, in dependency order. -------------------------------------------------
# Order matters: auth first (tenants/roles backbone), then the data/tool services. Each *__init.sql is idempotent.
step=1
for svc in auth llms guardrails xagent rag memory tool-registry skill-registry cypherx-a1 tool-flow-bridge; do
  dir="/migrations/$svc"
  echo "[$step/10] service: $svc  (dir $dir)"
  if [ ! -d "$dir" ]; then
    echo "      WARNING: $dir not mounted — skipping $svc." >&2
    step=$((step + 1))
    continue
  fi

  # Apply EVERY numbered migration in chronological (lexicographic) order: init + seed + ALL feature
  # migrations (e.g. *__outbox.sql, *__wp03_auth_completion.sql, *__llms_wp05_rate_limits.sql,
  # *__policy_authoring.sql, *__hybrid_fts.sql, *__scoring_validity_consolidation.sql, ...). The
  # YYYYMMDD_NNNN__ timestamp+sequence prefix makes a plain lexicographic sort the correct dependency
  # order (each service's init < its seed < later changes). Non-migration files never match the glob:
  # schema.sql has no numeric prefix; atlas.hcl / README.md are not .sql.
  #
  # NOTE: an earlier version globbed ONLY *__init.sql / *__seed.sql (two separate loops) and therefore
  # SILENTLY SKIPPED every feature migration, leaving incomplete schemas on a fresh DB (documented in
  # LOCAL_RUN_NOTES). The single [0-9]*__*.sql pass below is the durable fix — all migrations are
  # idempotent, so re-running against an already-migrated DB is a safe no-op.
  found=0
  for f in $(ls "$dir"/[0-9]*__*.sql 2>/dev/null | sort); do
    found=1
    run_file "$f"
  done
  [ "$found" -eq 0 ] && echo "      (no numbered migrations for $svc)"

  step=$((step + 1))
done

# --- Step 8: provision runtime-role passwords + per-role search_path (idempotent ALTER ROLE). ---------------------
# The init scripts create the *_user roles LOGIN but password-less and with no default search_path. Here we:
#   * ALTER ROLE <role> WITH LOGIN PASSWORD '<pw>'   — only if the *_DB_PASSWORD env var is non-empty.
#   * ALTER ROLE <role> SET search_path = <schema>, public   — always (idempotent; harmless if already set).
# The role MUST already exist (created by that service's init). We guard on pg_roles so a not-yet-migrated service
# is skipped rather than failing the whole run.
echo "[9/10] provision runtime-role passwords + search_path"

# set_role_pw <role> <schema> <password>
# Role + schema are OUR OWN constants (never user input), so it is safe to interpolate them straight into the SQL.
# The PASSWORD may contain arbitrary characters, so it is passed as a psql client variable and emitted via :'pw'
# (psql produces a correctly single-quoted, escaped SQL literal). NOTE: psql does NOT substitute :vars inside a
# dollar-quoted ($$...$$) body, so we keep the ALTER in PLAIN SQL (where :'pw' IS substituted) and guard role
# existence with a SEPARATE DO block that only references the constant role name.
set_role_pw() {
  role="$1"; schema="$2"; pw="$3"
  # 1) search_path — always (re)applied; idempotent; no secret. Guarded so a not-yet-migrated role is skipped.
  $PSQL "$MIGRATE_DATABASE_URL" -c \
    "DO \$\$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$role') THEN EXECUTE 'ALTER ROLE $role SET search_path = $schema, public'; END IF; END \$\$;"
  if [ -n "$pw" ]; then
    # 2) password — only when provided. The role is known to exist (its init ran above).
    # The password may contain arbitrary characters, so it is passed as a psql client variable
    # and emitted via :'pw' (psql produces a correctly single-quoted, escaped SQL literal).
    # IMPORTANT: psql does variable interpolation on input read from stdin / -f, but NOT on a -c
    # command string (a -c ":'pw'" fails with `syntax error at or near ":"`). So the statement is
    # piped via stdin (-f -), where :'pw' IS substituted. (This was the real fix for the step-8
    # provisioning bug noted in LOCAL_RUN_NOTES — the earlier -c form never interpolated.)
    printf '%s\n' "ALTER ROLE $role WITH LOGIN PASSWORD :'pw';" \
      | $PSQL "$MIGRATE_DATABASE_URL" --set=pw="$pw" -f -
    echo "  -> $role: password set + search_path=$schema"
  else
    echo "  -> $role: search_path=$schema (no password var set — left as-is)"
  fi
}

set_role_pw auth_user   auth       "${AUTH_DB_PASSWORD:-}"
set_role_pw llms_user   llms       "${LLMS_DB_PASSWORD:-}"
set_role_pw grd_user    guardrails "${GUARDRAILS_DB_PASSWORD:-}"
set_role_pw xagent_user xagent     "${XAGENT_DB_PASSWORD:-}"
set_role_pw rag_user    rag        "${RAG_DB_PASSWORD:-}"
set_role_pw mem_user    memory     "${MEM_DB_PASSWORD:-}"
set_role_pw tool_user   tools      "${TOOL_DB_PASSWORD:-}"
set_role_pw skill_user  skills     "${SKILL_DB_PASSWORD:-}"
set_role_pw cxa1_user   cypherx_a1 "${CYPHERXA1_DB_PASSWORD:-}"
set_role_pw flow_tools_user flow_tools "${FLOW_TOOLS_DB_PASSWORD:-}"

echo "[10/10] migrate complete"
echo "==================================================================================="
echo "CypherX migrate: DONE. All schemas/roles/RLS/seed applied; runtime-role search_path/passwords provisioned."
echo "==================================================================================="
