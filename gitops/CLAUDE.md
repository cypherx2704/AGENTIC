# CLAUDE.md — gitops

> ArgoCD **App-of-Apps** GitOps repo — the single source of truth for *what is deployed to each CypherX environment*. ArgoCD continuously reconciles cluster state to match this repo; humans change desired state only by merging a PR here (no human ever `kubectl apply`s a workload). Platform root guide: [../CLAUDE.md](../CLAUDE.md).

## What this is
Implements **Component 19** of `archive/Manoj/phases/phase-01-infrastructure.md` (sync-policy authority is Component 12 / ArgoCD; the cross-repo image-bump PR bot is Component 18). **Status: stub / scaffolding by design.** At Phase 1 end the three App-of-Apps *root* `Application`s exist (`apps/`) plus empty, well-commented per-env directories (`envs/`, `base/`) — but **zero service child apps on disk**. Service teams add their app under `envs/<env>/<namespace>/<service>/` in their own deploy phase (Phases 2–14). Canonical remote: `github.com/cypherx-ai/cypherx-gitops`.

## Tech stack
No application code, no Dockerfile, no DB, no tests, no CI of its own. Pure declarative GitOps:
- **ArgoCD** `Application` manifests (`apiVersion: argoproj.io/v1alpha1`), plain Kubernetes YAML.
- **App-of-Apps** pattern: one root `Application` per env watches that env's dir (`directory.recurse: true`) and auto-creates a child `Application` for every `*.yaml` committed beneath it.
- Child apps (added later) are *described* in `README.md` as pointing at a shared `cypherx-service` Helm chart with values from `base/<service>/` + an env overlay — that chart and overlay structure are **not present in this repo yet**.

## Repository layout
| Path | Holds |
|------|-------|
| `apps/dev-apps.yaml` | Root `Application` `cypherx-platform-dev` → watches `envs/dev/`, **auto-sync** (prune + selfHeal) |
| `apps/staging-apps.yaml` | Root `Application` `cypherx-platform-staging` → watches `envs/staging/`, **auto-sync** (prune + selfHeal) |
| `apps/prod-apps.yaml` | Root `Application` `cypherx-platform-prod` → watches `envs/prod/`, **MANUAL sync** (no `automated:` block) |
| `envs/{dev,staging,prod}/.gitkeep` | Per-env child-app dirs, currently only an annotated `.gitkeep`; each env maps 1:1 to a cluster (`cypherx-dev` / `cypherx-staging` / `cypherx-prod`, separate clusters/accounts — Component 4) |
| `base/.gitkeep` | Shared Helm value files / kustomize bases (annotated `.gitkeep` only today) |
| `README.md` | Authoritative usage, flow diagrams, sync-policy table, and guard notes |
| `.gitignore` | `.DS_Store`, `*.tgz`, `node_modules/` |

All three roots share: `metadata.namespace: argocd`, `finalizers: [resources-finalizer.argocd.io]`, labels `app.kubernetes.io/part-of: cypherx-platform` + `cypherx.ai/env: <env>`, `spec.project: default`, `source.repoURL: https://github.com/cypherx-ai/cypherx-gitops.git`, `targetRevision: main`, `path: envs/<env>`, `directory.recurse: true` with `include: "{*.yaml,**/*.yaml}"`, `destination.server: https://kubernetes.default.svc` / `namespace: argocd`, `syncOptions: [CreateNamespace=true, ApplyOutOfSyncOnly=true]`, and a 5-retry backoff (10s, factor 2, max 3m). **The only difference: dev & staging carry `syncPolicy.automated {prune: true, selfHeal: true}`; prod omits it.**

## Build, test, run
No build/test/package step (declarative config only). **Bootstrap once per cluster**; after that, deployment is purely merging PRs here:
```bash
kubectl apply -n argocd -f apps/dev-apps.yaml      # dev cluster
kubectl apply -n argocd -f apps/staging-apps.yaml  # staging cluster
kubectl apply -n argocd -f apps/prod-apps.yaml     # prod cluster
```
There is **no in-container service, no port, no healthz/readyz** in this repo — it is not a workload (the canonical 8080 / health-contract belongs to the services it deploys, not here). It does not appear in `infra/compose/docker-compose.yml`. ArgoCD itself runs in namespace `argocd` (Component 6 / 12 — bootstrapped before Istio, no sidecar injection).

## Configuration & secrets
No env vars are read by this repo (no runtime). Relevant external secrets (Doppler, never committed):
- `ci/github_app_private_key` — private key for the `cypherx-gitops-bot` **GitHub App** that opens image-bump PRs here (180-day rotation; CI reads it from Secrets Manager `cypherx/ci/gitops-bot/*`). Full token-exchange flow: `infra/ci/README.md`.
ArgoCD's connection to this repo uses an HTTPS deploy key (Component 12). No `.env`/`.env.example` and **no mock toggles** apply here (those are service concerns).

## Contracts & cross-repo dependencies
- Consumes **no `contracts/` schemas** directly — it deploys the services that honour the contracts; it owns no Kafka topics and no DB schema/role.
- **Component 18 ↔ 19 handshake:** a service repo's CI (`reusable-service-ci.yml`) builds + Trivy-scans + pushes an **immutable** image `cypherx/<service>:sha-<git-sha7>` to ECR, then `cypherx-gitops-bot` opens a PR here bumping `envs/<env>/<ns>/<service>/image.txt`. dev/staging PRs are **auto-merged** → ArgoCD auto-syncs; prod PRs are **left for human review** and even after merge wait for a manual sync.
- Future child app shape (per README, not yet on disk): `envs/<env>/<ns>/<service>/{<service>-app.yaml, image.txt}`; shared values under `base/<service>/`, env-specific overrides in the env overlay. Example namespaces: `envs/dev/shared-core/auth/`, `.../shared-core/llms/`, `envs/dev/xagent/agent-runtime/`.

## Invariants & guards (do NOT break)
- **NEVER add `syncPolicy.automated` to `apps/prod-apps.yaml` or to any app under `envs/prod/`.** Its absence is the prod safety gate — prod child apps are auto-*created* (tree matches git) but each sync waits for a human (`argocd app sync` / ArgoCD UI). Adding `automated:` silently turns prod into continuous deploy and defeats Component 12. (Reiterated in `apps/prod-apps.yaml` and `envs/prod/.gitkeep`.)
- Image tags written here are **always immutable** (`sha-<sha7>` or `<semver>`), **never `:latest`** — so every ArgoCD sync is fully reproducible.
- The CI bot uses a GitHub **App installation token**, **never a personal PAT** (deployments must not be tied to one human).
- Keep `CreateNamespace=true` on **all** roots (incl. prod) so a human-approved first sync needs no manual namespace prep.
- Keep `finalizers: [resources-finalizer.argocd.io]` on each root so it isn't orphan-pruned.
- Empty dirs are held by annotated `.gitkeep` — replace with real apps per phase; **don't delete the `envs/` or `base/` scaffolding** and don't strip the `.gitkeep` guidance comments.

## Gotchas & current status
- **Intentionally near-empty.** `envs/{dev,staging,prod}/` and `base/` contain only `.gitkeep` (each carries useful comments on where/how to add apps); there are **zero service child apps today**. Correct for Phase 1, not an omission.
- `dev` and `staging` roots are byte-for-byte identical except `metadata.name`, the `cypherx.ai/env` label, and `path`; only `prod` differs (no `automated:` block).
- `development` and `feature/base-implementation` branches are identical for this repo; HEAD commit on `development` is "Phase 1 — gitops App-of-Apps scaffolding".
- The `cypherx-service` Helm chart and per-env overlay layout are *described* in `README.md` but **do not exist in this repo yet** — they arrive with the first service deploy phase. Don't assume they're present.
- Config-only: no Dockerfile, no Helm charts, no tests, no CI workflows live in this repo.
