# CypherX GitOps (`cypherx-gitops`)

The single source of truth for **what is deployed to each CypherX environment**.
ArgoCD continuously reconciles the cluster state to match this repo. Nothing is
`kubectl apply`-ed by humans — desired state is changed by merging a PR here, and
ArgoCD does the rest.

> Component 19 of `archive/Manoj/phases/phase-01-infrastructure.md`.
> Sync policy authority: Component 12 (ArgoCD). Cross-repo PR bot: Component 18.

At **Phase 1 end this repo is intentionally near-empty** — the App-of-Apps roots
and the env scaffolding exist, but no service apps yet. Service teams add their
app under `envs/<env>/<namespace>/<service>/` in their own deploy phase
(Phases 2–14).

---

## Layout

```
cypherx-gitops/
├── apps/
│   ├── dev-apps.yaml        ← ArgoCD App-of-Apps ROOT for dev     (watches envs/dev/,   auto-sync)
│   ├── staging-apps.yaml    ← ArgoCD App-of-Apps ROOT for staging (watches envs/staging/, auto-sync)
│   └── prod-apps.yaml       ← ArgoCD App-of-Apps ROOT for prod    (watches envs/prod/,  MANUAL sync)
├── envs/
│   ├── dev/      (.gitkeep — apps added per phase: envs/dev/<ns>/<service>/)
│   ├── staging/  (.gitkeep)
│   └── prod/     (.gitkeep)
└── base/
    └── (.gitkeep — shared Helm value files / kustomize bases, added per phase)
```

---

## App-of-Apps flow

Each environment has exactly one **root** `Application` (in `apps/`). The root
watches that env's directory and **auto-creates a child `Application` for every
manifest committed under it** — the App-of-Apps pattern. You bootstrap a cluster
by applying just the root once; everything else follows from git.

```
            ┌────────────────────────────────────────────────────────────┐
            │  apps/dev-apps.yaml  →  Application "cypherx-platform-dev"  │
            │     spec.source.path = envs/dev   (recurse: true)          │
            └───────────────────────────┬────────────────────────────────┘
                                        │ ArgoCD reconciles every *.yaml under envs/dev/
              ┌─────────────────────────┼─────────────────────────────┐
              ▼                         ▼                             ▼
   envs/dev/shared-core/auth/   envs/dev/shared-core/llms/   envs/dev/xagent/agent-runtime/
   Application "auth-app"        Application "llms-app"       Application "agent-runtime-app"
   (added in Phase 2)           (added in Phase 3)           (added in Phase 6)
```

**Bootstrap (once per cluster):**

```bash
# dev cluster
kubectl apply -n argocd -f apps/dev-apps.yaml
# staging cluster
kubectl apply -n argocd -f apps/staging-apps.yaml
# prod cluster
kubectl apply -n argocd -f apps/prod-apps.yaml
```

After that, deployments happen purely by merging PRs into this repo.

---

## Sync policies (Component 12)

| Env | Child apps created | Sync | Self-heal / prune | Who triggers a roll |
|-----|--------------------|------|-------------------|---------------------|
| **dev** | automatically | **automated** | yes | CI auto-merges the gitops PR → ArgoCD syncs immediately |
| **staging** | automatically | **automated** | yes | CI auto-merges the gitops PR → ArgoCD syncs immediately |
| **prod** | automatically | **MANUAL** | no auto-sync | a human approves each sync in the ArgoCD UI / `argocd app sync` |

- **dev & staging** roots carry `syncPolicy.automated { prune: true, selfHeal: true }`.
  A merged image-tag bump rolls out with no human action.
- **prod** root has **no `automated:` block**. ArgoCD keeps the child apps
  *created and in sync with git's structure*, but each actual sync waits for a
  human. **Do not add `syncPolicy.automated` to `apps/prod-apps.yaml` or to any
  app under `envs/prod/`** — that is the prod safety gate and removing it turns
  prod into continuous deploy.

---

## How image updates land here (Component 18 ↔ 19 handshake)

```
service repo CI (reusable-service-ci.yml)
   │  build + Trivy + push ECR  →  cypherx/<service>:sha-<git-sha7>   (immutable)
   │  on merge to main:
   ▼
cypherx-gitops-bot (GitHub App, short-lived installation token)
   │  opens a PR here bumping envs/dev/<ns>/<service>/image.txt → sha-<git-sha7>
   ▼
PR auto-merged (dev/staging)  ──►  ArgoCD auto-syncs the child app  ──►  rollout
PR left for review (prod)     ──►  human approves sync in ArgoCD    ──►  rollout
```

- Image tags written here are always **immutable** (`sha-<sha7>` or `<semver>`),
  never `:latest` — so an ArgoCD sync is fully reproducible.
- The bot uses a GitHub **App** installation token (not a PAT). Its private key
  lives in Doppler `ci/github_app_private_key` (180-day rotation) and is read by
  CI from Secrets Manager `cypherx/ci/gitops-bot/*`. See
  `infra/ci/README.md` for the full token-exchange flow.

---

## Adding a service app (per-phase, for reference)

A service's deploy phase adds, per env it targets, a directory like:

```
envs/dev/shared-core/auth/
  ├── auth-app.yaml     ← ArgoCD Application: points at the cypherx-service Helm chart,
  │                        values from base/auth/ + this env overlay
  └── image.txt         ← single line: the immutable image tag the CI bot bumps
```

The dev/staging copies inherit auto-sync from their root. The prod copy MUST NOT
declare `syncPolicy.automated`. Shared values go in `base/<service>/`; per-env
overrides live in the env overlay.

---

## Conventions

- Repo: `github.com/cypherx-ai/cypherx-gitops` (referenced by the ArgoCD roots
  and by the CI bot).
- ArgoCD namespace: `argocd` (Component 6 / Component 12 — bootstrapped before
  Istio, no sidecar injection).
- Envs map 1:1 to clusters: `cypherx-dev`, `cypherx-staging`, `cypherx-prod`
  (separate clusters/accounts — Component 4).
- Empty dirs are held by `.gitkeep`; replace with real apps per phase.
