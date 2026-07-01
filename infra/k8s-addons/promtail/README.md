# k8s-addons/promtail — Component 13

Installs the Promtail **DaemonSet** (grafana/promtail chart) that collects all pod
stdout/stderr, parses Contract 6 JSON logs, and ships them to Loki.

## Label discipline (the load-bearing part)

**Allowed Loki labels (low-cardinality ONLY):**
`namespace`, `pod`, `container`, `service`, `level`, `environment`

**Forbidden as labels (high-cardinality — JSON fields, queried at query time):**
`tenant_id`, `agent_id`, `request_id`, `trace_id`, `span_id`

> **OOM rationale (do NOT "fix" this by adding tenant_id as a label):** every Loki
> label *value* is a distinct active stream. With 1000 tenants × 20 pods × N
> containers, promoting `tenant_id` to a label creates 20k+ active streams per
> service and Loki OOMs. These fields stay inside the JSON line and are filtered
> at query time:
>
> ```
> {service="xagent"} | json | tenant_id="<uuid>"
> ```

The pipeline: `cri` → `json` (extract `level`, `service`, and — *without*
labelling them — `tenant_id`/`agent_id`/`request_id`/`trace_id`/`span_id`) →
`labels` (promotes ONLY `level`, `service`) → `static_labels` (`environment`).
`namespace`/`pod`/`container` come from `relabel_configs` on the Kubernetes SD
metadata. Unparseable lines are tagged `parse_error="true"` (not dropped) so the
Contract 15 #9 smoke test can assert zero parse errors.

`tenant_id` is set as the Loki `X-Scope-OrgID` from the parsed field — multi-tenant
routing, **not** a label.

## DaemonSet coverage

`tolerations: [{ operator: Exists }]` so Promtail also runs on the tainted
`system-nodes` (`CriticalAddonsOnly`) and `observability` nodes — no node's logs
are lost.

## Smoke test hooks

- Assertion #2: echo-service log line visible in Loki within 10s.
- Assertion #3: `logcli labels` excludes the forbidden set → see `forbidden_labels`
  output.

## Inputs (highlights)

| Variable        | Default  | Notes |
|-----------------|----------|-------|
| `chart_version` | `6.16.6` | Chart pin. |
| `loki_push_url` | derived  | In-cluster Loki gateway push endpoint. |
| `environment`   | —        | Static `environment` label value. |
