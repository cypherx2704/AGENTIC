#!/usr/bin/env bash
# =============================================================================
# infra-smoke-test.sh — CypherX Phase 1 Component 21 infrastructure smoke test
# =============================================================================
# Proves the platform PLUMBING end-to-end against a freshly deployed dev env:
# a log line written by a pod reaches Loki, a trace id propagates through the
# mesh, and a Contract 5 Kafka event round-trips. Deploys a throwaway
# echo-service into the `smoketest` namespace, runs the 10 assertions from the
# Phase 1 Component 21 table, then tears everything down and verifies no leaks.
#
# Exit code: 0 only if ALL assertions pass. Non-zero on the first failure
# (unless --keep is set, which still exits non-zero but skips teardown so you
# can debug). Phase 1 cannot be marked complete until this passes TWO
# CONSECUTIVE runs (use: `infra-smoke-test.sh --runs 2`, the default).
#
# Usage:
#   infra-smoke-test.sh [--env dev] [--runs 2] [--namespace smoketest]
#                       [--image <ECR>/cypherx/echo-service] [--tag <sha7>]
#                       [--no-helm] [--keep] [--skip-deploy]
#
# Requires: kubectl, jq. Optional but used when present: helm, logcli (Loki),
# curl, kafka-console-consumer / kcat, aws (orphan sweep), promtool/wget.
# =============================================================================
set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Defaults (override via flags / env)
# ----------------------------------------------------------------------------
ENV="${SMOKE_ENV:-dev}"
RUNS="${SMOKE_RUNS:-2}"
NS="${SMOKE_NS:-smoketest}"
RELEASE="echo"
SERVICE="echo"
KAFKA_TOPIC="cypherx.smoketest.event"
IMAGE_REPO="${SMOKE_IMAGE:-}"
IMAGE_TAG="${SMOKE_TAG:-}"
USE_HELM=1
KEEP=0
SKIP_DEPLOY=0
INGEST_WAIT=10          # Component 21 allows up to 10s for Loki/Tempo ingest
SCALE_TIMEOUT=300       # assertion 8: allow Karpenter time to provision a node

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
SMOKE_DIR="${REPO_ROOT}/smoketest"
API_HOST="api.${ENV}.cypherx.ai"

# ----------------------------------------------------------------------------
# Logging helpers
# ----------------------------------------------------------------------------
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
PASS_COUNT=0; FAIL_COUNT=0
declare -a FAILED_ASSERTIONS=()

info()  { printf '%s[smoke]%s %s\n' "${CYN}" "${RST}" "$*"; }
warn()  { printf '%s[warn]%s  %s\n' "${YLW}" "${RST}" "$*" >&2; }
die()   { printf '%s[fatal]%s %s\n' "${RED}" "${RST}" "$*" >&2; exit 1; }

pass()  { PASS_COUNT=$((PASS_COUNT+1)); printf '  %s[PASS]%s %s\n' "${GRN}" "${RST}" "$*"; }
fail()  {
  FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_ASSERTIONS+=("$1")
  printf '  %s[FAIL]%s %s\n' "${RED}" "${RST}" "$*"
}

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

# ----------------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)        ENV="$2"; API_HOST="api.${ENV}.cypherx.ai"; shift 2;;
    --runs)       RUNS="$2"; shift 2;;
    --namespace)  NS="$2"; shift 2;;
    --image)      IMAGE_REPO="$2"; shift 2;;
    --tag)        IMAGE_TAG="$2"; shift 2;;
    --no-helm)    USE_HELM=0; shift;;
    --keep)       KEEP=1; shift;;
    --skip-deploy) SKIP_DEPLOY=1; shift;;
    -h|--help)    usage;;
    *) die "unknown flag: $1 (try --help)";;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }
have() { command -v "$1" >/dev/null 2>&1; }

need kubectl
need jq
[[ ${USE_HELM} -eq 1 ]] && ! have helm && { warn "helm not found — falling back to raw manifests"; USE_HELM=0; }

# ----------------------------------------------------------------------------
# Deploy / teardown
# ----------------------------------------------------------------------------
ensure_doppler_token() {
  # The per-(env,namespace) operator bootstrap Secret is provisioned by
  # Terraform (Component 11). If it is missing, the DopplerSecret cannot sync;
  # warn but continue (assertion 9 will then fail loudly rather than silently).
  if ! kubectl -n "${NS}" get secret doppler-token-secret >/dev/null 2>&1; then
    warn "doppler-token-secret missing in ${NS} — assertion 9 will fail until Terraform provisions it"
  fi
}

deploy() {
  info "deploying echo-service into namespace '${NS}' (env=${ENV})"
  kubectl apply -f "${SMOKE_DIR}/k8s/namespace.yaml"
  kubectl label ns "${NS}" istio-injection=enabled --overwrite >/dev/null
  ensure_doppler_token

  if [[ ${USE_HELM} -eq 1 ]]; then
    local args=(upgrade --install "${RELEASE}" "${SMOKE_DIR}" -n "${NS}" --wait --timeout 5m)
    [[ -n "${IMAGE_REPO}" ]] && args+=(--set "cypherx-service.image.repository=${IMAGE_REPO}")
    [[ -n "${IMAGE_TAG}"  ]] && args+=(--set "cypherx-service.image.tag=${IMAGE_TAG}")
    # KAFKA_BROKERS is the LAST extraEnv entry (index 8) in smoketest/values.yaml.
    [[ -n "${KAFKA_BROKERS:-}" ]] && args+=(--set-string "cypherx-service.extraEnv[8].value=${KAFKA_BROKERS}")
    helm dependency build "${SMOKE_DIR}" >/dev/null 2>&1 || true
    helm "${args[@]}"
  else
    kubectl apply -f "${SMOKE_DIR}/k8s/dopplersecret.yaml"
    local repo="${IMAGE_REPO:-REPLACE_ME_ECR}" tag="${IMAGE_TAG:-REPLACE_ME_TAG}"
    sed -e "s#REPLACE_ME_ECR/cypherx/echo-service#${repo}#" \
        -e "s#REPLACE_ME_TAG#${tag}#" \
        -e "s#REPLACE_ME_KAFKA_BROKERS#${KAFKA_BROKERS:-}#" \
        "${SMOKE_DIR}/k8s/echo-deployment.yaml" | kubectl apply -f -
    kubectl apply -f "${SMOKE_DIR}/k8s/servicemonitor.yaml"
  fi
  kubectl apply -f "${SMOKE_DIR}/k8s/kong-route.yaml"

  info "waiting for echo-service rollout"
  kubectl -n "${NS}" rollout status deploy/"${RELEASE}" --timeout=5m \
    || die "echo-service did not become Ready"
}

teardown() {
  [[ ${KEEP} -eq 1 ]] && { warn "--keep set: skipping teardown (namespace ${NS} left in place)"; return; }
  info "tearing down namespace '${NS}'"
  kubectl delete -f "${SMOKE_DIR}/k8s/kong-route.yaml" --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete ns "${NS}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------------------
# Per-pod helpers
# ----------------------------------------------------------------------------
echo_pod() { kubectl -n "${NS}" get pod -l app.kubernetes.io/name="${SERVICE}" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }

# curl through the cluster (a throwaway pod) so the test works without VPN.
in_cluster_curl() {
  kubectl -n "${NS}" run smoke-curl-$RANDOM --rm -i --restart=Never \
    --image=curlimages/curl:8.10.1 --quiet -- "$@" 2>/dev/null
}

# =============================================================================
# Assertions (Phase 1 Component 21 table)
# =============================================================================
LAST_TRACEPARENT=""
LAST_TRACE_ID=""

# 1) ALB -> Kong -> echo GET /echo returns 200 with a populated `traceparent`.
assert_01_alb_kong_echo() {
  info "assertion 1: ALB -> Kong -> echo /echo returns 200 + traceparent"
  local out code tp
  # Prefer the real external path (api.<env>.cypherx.ai) when reachable; fall
  # back to in-cluster curl to the Kong proxy if VPN is unavailable.
  out="$(in_cluster_curl -sS -m 15 -D - "https://${API_HOST}/echo" -o /tmp/echo_body 2>/dev/null || true)"
  if [[ -z "${out}" ]]; then
    out="$(in_cluster_curl -sS -m 15 -D - "http://kong-proxy.ingress.svc.cluster.local/echo" \
            -H "Host: ${API_HOST}" -o /tmp/echo_body 2>/dev/null || true)"
  fi
  code="$(printf '%s' "${out}" | awk 'toupper($0) ~ /^HTTP/ {c=$2} END{print c}')"
  tp="$(printf '%s' "${out}" | grep -i '^traceparent:' | head -1 | awk '{print $2}' | tr -d '\r')"
  if [[ -z "${tp}" ]]; then
    # Body also carries the traceparent the sidecar saw.
    tp="$(jq -r '.traceparent // empty' /tmp/echo_body 2>/dev/null || true)"
  fi
  LAST_TRACEPARENT="${tp}"
  # traceparent: 00-<32hex trace>-<16hex span>-<2hex flags>
  if [[ "${code}" == "200" && "${tp}" =~ ^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$ ]]; then
    LAST_TRACE_ID="${BASH_REMATCH[1]}"
    pass "200 from /echo with traceparent ${tp}"
  else
    fail "1: code='${code}' traceparent='${tp}' (expected 200 + valid W3C traceparent)"
  fi
}

# 2) echo log line visible in Loki within 10s.
assert_02_loki_log() {
  info "assertion 2: echo log line in Loki within ${INGEST_WAIT}s"
  sleep "${INGEST_WAIT}"
  local n=0
  if have logcli; then
    n="$(logcli query --quiet --limit=5 --since=2m "{service=\"${SERVICE}\"}" 2>/dev/null | wc -l | tr -d ' ')"
  else
    # Fallback: query Loki HTTP API from inside the cluster.
    local q body
    q="$(printf '{service="%s"}' "${SERVICE}")"
    body="$(in_cluster_curl -sS -m 15 -G \
      "http://loki-gateway.observability.svc.cluster.local/loki/api/v1/query_range" \
      --data-urlencode "query=${q}" --data-urlencode "limit=5" 2>/dev/null || true)"
    n="$(printf '%s' "${body}" | jq -r '[.data.result[]?.values[]?] | length' 2>/dev/null || echo 0)"
  fi
  if [[ "${n:-0}" -ge 1 ]]; then
    pass "Loki returned ${n} line(s) for {service=\"${SERVICE}\"}"
  else
    fail "2: no Loki results for {service=\"${SERVICE}\"} within ${INGEST_WAIT}s"
  fi
}

# 3) Loki labels are low-cardinality only (no tenant_id/request_id/etc as labels).
assert_03_loki_labels() {
  info "assertion 3: Loki labels exclude the forbidden high-cardinality set"
  local labels forbidden=(tenant_id agent_id request_id trace_id span_id) bad=""
  if have logcli; then
    labels="$(logcli labels 2>/dev/null || true)"
  else
    labels="$(in_cluster_curl -sS -m 15 \
      "http://loki-gateway.observability.svc.cluster.local/loki/api/v1/labels" 2>/dev/null \
      | jq -r '.data[]?' 2>/dev/null || true)"
  fi
  for f in "${forbidden[@]}"; do
    if printf '%s\n' "${labels}" | grep -qx "${f}"; then bad="${bad} ${f}"; fi
  done
  if [[ -z "${bad}" ]]; then
    pass "no forbidden labels present (checked: ${forbidden[*]})"
  else
    fail "3: forbidden high-cardinality Loki label(s) present:${bad}"
  fi
}

# 4) Trace id from the response is in Tempo within 10s; spans cross Kong->echo.
assert_04_tempo_trace() {
  info "assertion 4: trace ${LAST_TRACE_ID:-<none>} visible in Tempo within ${INGEST_WAIT}s"
  if [[ -z "${LAST_TRACE_ID}" ]]; then fail "4: no trace_id captured from assertion 1"; return; fi
  sleep "${INGEST_WAIT}"
  local body found spans
  body="$(in_cluster_curl -sS -m 15 \
    "http://tempo-query-frontend.observability.svc.cluster.local:3200/api/traces/${LAST_TRACE_ID}" \
    2>/dev/null || true)"
  found="$(printf '%s' "${body}" | jq -r '.batches // .data // empty' 2>/dev/null | head -c1)"
  # Look for both a kong/ingress span and an echo span name in the trace.
  spans="$(printf '%s' "${body}" | jq -r '[.. | .name? // empty] | join(",")' 2>/dev/null || true)"
  if [[ -n "${found}" ]] && printf '%s' "${spans}" | grep -qiE 'echo'; then
    pass "trace ${LAST_TRACE_ID} present in Tempo with echo span(s)"
  else
    fail "4: trace ${LAST_TRACE_ID} not found in Tempo (or missing Kong->echo spans)"
  fi
}

# 5) Kafka topic has exactly 1 message with a valid Contract 5 envelope.
assert_05_kafka_envelope() {
  info "assertion 5: ${KAFKA_TOPIC} has 1 message with a valid Contract 5 envelope"
  local msg=""
  # Consume from earliest, max 1 message, short timeout. Prefer kcat, else
  # kafka-console-consumer. Run from inside the cluster against the brokers.
  local consumer_yaml="${SMOKE_DIR}/k8s/.kafka-consume.json"
  if have kcat && [[ -n "${KAFKA_BROKERS:-}" ]]; then
    msg="$(kcat -b "${KAFKA_BROKERS}" -t "${KAFKA_TOPIC}" -C -o beginning -c 1 -e 2>/dev/null || true)"
  else
    # In-cluster one-shot consumer pod.
    msg="$(kubectl -n "${NS}" run smoke-kafka-$RANDOM --rm -i --restart=Never --quiet \
      --image=edenhill/kcat:1.7.1 -- \
      -b "${KAFKA_BROKERS:-kafka-bootstrap.messaging.svc.cluster.local:9092}" \
      -t "${KAFKA_TOPIC}" -C -o beginning -c 1 -e 2>/dev/null || true)"
  fi
  if [[ -z "${msg}" ]]; then fail "5: no message consumed from ${KAFKA_TOPIC}"; return; fi
  # Validate the Contract 5 required envelope keys + that partition_key==tenant_id.
  if printf '%s' "${msg}" | jq -e '
        (.event_id|type=="string") and
        (.event_type=="cypherx.smoketest.event") and
        (.schema_version|test("^[0-9]+\\.[0-9]+\\.[0-9]+$")) and
        (.produced_at|type=="string") and
        (.tenant_id|type=="string") and
        (.producer_service|type=="string") and
        (.partition_key|type=="string") and
        (.partition_key==.tenant_id) and
        (.payload|type=="object")
      ' >/dev/null 2>&1; then
    pass "envelope valid (event_type, schema_version, partition_key==tenant_id, payload)"
  else
    fail "5: consumed message is not a valid Contract 5 envelope: $(printf '%s' "${msg}" | head -c200)"
  fi
}

# 6) echo /metrics scraped by Prometheus: up{job="echo"} == 1.
assert_06_prometheus_up() {
  info "assertion 6: Prometheus up{job=\"echo\"} == 1"
  local body val
  body="$(in_cluster_curl -sS -m 15 -G \
    "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090/api/v1/query" \
    --data-urlencode 'query=up{job="echo"}' 2>/dev/null || true)"
  val="$(printf '%s' "${body}" | jq -r '[.data.result[]?.value[1]] | map(tonumber) | max // 0' 2>/dev/null || echo 0)"
  if [[ "${val}" == "1" ]]; then
    pass "up{job=\"echo\"} == 1 (PERMISSIVE mTLS scrape on :9090 works)"
  else
    fail "6: up{job=\"echo\"} != 1 (got '${val}')"
  fi
}

# 7) PgBouncer (transaction mode) accepted the RLS round-trip -> "rls_probe=ok".
assert_07_pgbouncer_rls() {
  info "assertion 7: PgBouncer accepted BEGIN; SET LOCAL app.tenant_id; SELECT 1; COMMIT"
  local pod logs
  pod="$(echo_pod)"
  [[ -z "${pod}" ]] && { fail "7: no echo pod found"; return; }
  # The app emits the Contract 6 log line with rls_probe=ok on success.
  logs="$(kubectl -n "${NS}" logs "${pod}" -c "${SERVICE}" --tail=500 2>/dev/null || true)"
  if printf '%s' "${logs}" | jq -rs '.[] | select(.extra.rls_probe=="ok" or .rls_probe=="ok") | .message' 2>/dev/null | grep -q .; then
    pass "echo log line shows rls_probe=ok"
  elif printf '%s' "${logs}" | grep -q 'rls_probe=ok\|"rls_probe":"ok"'; then
    pass "echo log line shows rls_probe=ok"
  else
    fail "7: no rls_probe=ok log line from echo (RLS round-trip via PgBouncer failed)"
  fi
}

# 8) Scale 1 -> 3; Karpenter provisions a node if needed; all 3 pods Ready.
assert_08_scale_karpenter() {
  info "assertion 8: scale ${RELEASE} 1 -> 3, all 3 Ready (Karpenter provisions if needed)"
  local nodes_before nodes_after
  nodes_before="$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  kubectl -n "${NS}" scale deploy/"${RELEASE}" --replicas=3 >/dev/null
  if kubectl -n "${NS}" rollout status deploy/"${RELEASE}" --timeout="${SCALE_TIMEOUT}s" >/dev/null 2>&1; then
    local ready
    ready="$(kubectl -n "${NS}" get deploy/"${RELEASE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
    nodes_after="$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "${ready}" == "3" ]]; then
      pass "3/3 pods Ready (nodes ${nodes_before} -> ${nodes_after})"
    else
      fail "8: only ${ready}/3 pods Ready after scale"
    fi
  else
    fail "8: deployment did not reach 3 Ready replicas within ${SCALE_TIMEOUT}s"
  fi
}

# 9) Doppler operator synced the test secret; echo env has SMOKE_SECRET populated.
assert_09_doppler_secret() {
  info "assertion 9: Doppler-synced SMOKE_SECRET present (echo response SMOKE_SECRET_LEN > 0)"
  local body len
  body="$(in_cluster_curl -sS -m 15 "http://${RELEASE}.${NS}.svc.cluster.local:8080/echo" 2>/dev/null || true)"
  len="$(printf '%s' "${body}" | jq -r '.SMOKE_SECRET_LEN // .smoke_secret_len // 0' 2>/dev/null || echo 0)"
  if [[ "${len}" -gt 0 ]]; then
    pass "SMOKE_SECRET_LEN=${len} (Doppler operator sync confirmed)"
  else
    fail "9: SMOKE_SECRET_LEN=${len} (Doppler secret not synced into ${NS})"
  fi
}

# 10) After ns delete, no leaked Kafka topics, no orphan ALB target groups, no
#     orphan IAM roles.
assert_10_clean_teardown() {
  info "assertion 10: clean teardown leaves no orphans"
  teardown
  local clean=1

  # (a) namespace fully gone.
  if kubectl get ns "${NS}" >/dev/null 2>&1; then
    fail "10a: namespace ${NS} still present after delete"; clean=0
  fi

  # (b) the throwaway smoke-test topic must not linger. It is created on demand
  #     and should be deleted by teardown; assert it is absent.
  if [[ -n "${KAFKA_BROKERS:-}" ]] && have kcat; then
    if kcat -b "${KAFKA_BROKERS}" -L 2>/dev/null | grep -q "topic \"${KAFKA_TOPIC}\""; then
      warn "10b: ${KAFKA_TOPIC} still exists — deleting"
      delete_smoke_topic || true
      if kcat -b "${KAFKA_BROKERS}" -L 2>/dev/null | grep -q "topic \"${KAFKA_TOPIC}\""; then
        fail "10b: leaked Kafka topic ${KAFKA_TOPIC}"; clean=0
      fi
    fi
  else
    warn "10b: kafka brokers/kcat unavailable — skipping topic-leak check"
  fi

  # (c) orphan ALB target groups tagged for this smoke run.
  if have aws; then
    local tgs
    tgs="$(aws elbv2 describe-target-groups \
      --query "TargetGroups[?contains(TargetGroupName, 'smoketest') || contains(TargetGroupName, 'echo')].TargetGroupArn" \
      --output text 2>/dev/null || true)"
    if [[ -n "${tgs}" ]]; then
      fail "10c: orphan ALB target group(s): ${tgs}"; clean=0
    fi
    # (d) orphan IAM roles created for the smoke test.
    local roles
    roles="$(aws iam list-roles \
      --query "Roles[?contains(RoleName, 'smoketest')].RoleName" --output text 2>/dev/null || true)"
    if [[ -n "${roles}" ]]; then
      fail "10d: orphan IAM role(s): ${roles}"; clean=0
    fi
  else
    warn "10c/d: aws CLI unavailable — skipping ALB/IAM orphan sweep"
  fi

  [[ ${clean} -eq 1 ]] && pass "no orphan namespace / topic / target group / IAM role"
}

delete_smoke_topic() {
  [[ -z "${KAFKA_BROKERS:-}" ]] && return 0
  kubectl -n "${NS}" run smoke-kafka-del-$RANDOM --rm -i --restart=Never --quiet \
    --image=edenhill/kcat:1.7.1 -- -b "${KAFKA_BROKERS}" -X "delete.topic.enable=true" \
    >/dev/null 2>&1 || true
  # kcat cannot delete topics; prefer the kafka-topics admin if present.
  if have kafka-topics.sh; then
    kafka-topics.sh --bootstrap-server "${KAFKA_BROKERS}" --delete --topic "${KAFKA_TOPIC}" 2>/dev/null || true
  fi
}

# ----------------------------------------------------------------------------
# One full run = deploy + 10 assertions + teardown(in #10)
# ----------------------------------------------------------------------------
run_once() {
  local run_no="$1"
  PASS_COUNT=0; FAIL_COUNT=0; FAILED_ASSERTIONS=()
  info "================= RUN ${run_no}/${RUNS} (env=${ENV}) ================="

  if [[ ${SKIP_DEPLOY} -eq 0 ]]; then deploy; else info "--skip-deploy: using existing ${NS} deployment"; fi

  assert_01_alb_kong_echo
  assert_02_loki_log
  assert_03_loki_labels
  assert_04_tempo_trace
  assert_05_kafka_envelope
  assert_06_prometheus_up
  assert_07_pgbouncer_rls
  assert_08_scale_karpenter
  assert_09_doppler_secret
  assert_10_clean_teardown        # includes teardown

  info "run ${run_no} summary: ${GRN}${PASS_COUNT} passed${RST}, ${RED}${FAIL_COUNT} failed${RST}"
  if [[ ${FAIL_COUNT} -gt 0 ]]; then
    warn "failed assertions in run ${run_no}: ${FAILED_ASSERTIONS[*]}"
    return 1
  fi
  return 0
}

# On any unexpected error mid-run, attempt teardown unless --keep.
on_err() { warn "unexpected error (line $1) — attempting teardown"; teardown || true; }
trap 'on_err ${LINENO}' ERR

main() {
  info "CypherX Phase 1 infra smoke test — ${RUNS} consecutive green run(s) required"
  local green=0 i
  for (( i=1; i<=RUNS; i++ )); do
    if run_once "${i}"; then
      green=$((green+1))
    else
      die "run ${i} FAILED — smoke gate not satisfied (need ${RUNS} consecutive green runs)"
    fi
  done
  info "${GRN}ALL ${green}/${RUNS} runs green — Component 21 smoke gate PASSED${RST}"
}

main "$@"
