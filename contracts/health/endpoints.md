# Contract 7 — Health & Metrics Endpoints ⚡

> **Status:** ⚡ First-cycle. Every service must expose these three endpoints.
> **No service goes to staging without them.**

Every service MUST expose `/livez`, `/readyz` and `/metrics`.

---

## Critical separation: liveness vs readiness

> **Liveness MUST NOT depend on downstreams.** A momentary DB blip must not cause Kubernetes
> to kill an otherwise-healthy pod. **Readiness DOES check downstreams** and removes the pod
> from the load balancer until they recover.

| Concern | `/livez` (liveness) | `/readyz` (readiness) |
|---------|---------------------|------------------------|
| Question answered | Is the process alive and the event loop responsive? | Are dependencies healthy so the pod can serve traffic? |
| Checks downstreams (DB, Kafka, external deps)? | **NEVER** | **YES** |
| K8s probe that targets it | `livenessProbe` | `readinessProbe` |
| Effect of failure | Pod is **restarted** | Pod is **pulled from the service** (load balancer) until healthy |
| When it returns 503 | Only if the process itself is broken (deadlock, OOM imminent) | Whenever any required downstream is unhealthy |

---

## `GET /livez` — liveness

Liveness: process is alive, event loop responsive.

- **Response 200** — the process is alive:

  ```json
  { "status": "ok", "version": "1.2.3", "uptime_seconds": 3600 }
  ```

- **Response 503** — only if the process itself is broken (deadlock, OOM imminent).
- The Kubernetes `livenessProbe` targets this endpoint.
- It **NEVER** checks DB / Kafka / external deps.

---

## `GET /readyz` — readiness

Readiness: dependencies healthy, can serve traffic.

- **Response 200** — ready:

  ```json
  { "ready": true, "checks": { "database": "ok", "kafka": "ok" } }
  ```

- **Response 503** — not ready (at least one dependency unhealthy):

  ```json
  { "ready": false, "checks": { "database": "failed", "kafka": "ok" } }
  ```

- The Kubernetes `readinessProbe` targets this endpoint.
- The pod is **pulled from the service when 503** is returned, and re-added once it recovers.
- The `checks` object enumerates each required downstream and its per-dependency status.

---

## `GET /metrics` — Prometheus exposition

- **Content-Type:** `text/plain; version=0.0.4`
- **Body:** Prometheus exposition format.
- **Standard metrics** include `http_requests_total`, `http_request_duration_seconds`, etc.
- **Access:** restricted to in-cluster scrapers **via NetworkPolicy** (Prometheus namespace
  only). The endpoint MUST NOT be reachable from outside the cluster.

---

## Legacy `/health` alias rule

> Legacy alias `GET /health` **MAY** be exposed and **MUST** behave identically to `/livez`.
> New services **SHOULD NOT** expose `/health` — use `/livez` and `/readyz` explicitly.

| Endpoint | Status | Behaviour |
|----------|--------|-----------|
| `/health` | Legacy alias, optional | If present, MUST be identical to `/livez`. New services SHOULD NOT expose it. |
