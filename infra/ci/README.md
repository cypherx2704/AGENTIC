# CI/CD — GitHub Actions Base Workflows (Component 18)

This directory documents the CypherX CI/CD model. The workflows themselves live
in [`infra/.github/workflows/`](../.github/workflows/):

| File | Purpose |
|------|---------|
| `reusable-service-ci.yml` | `workflow_call` pipeline every service repo invokes: lint → unit test → multi-stage docker build → Trivy scan (fail CRITICAL) → push ECR → gitops PR on main. |
| `example-caller-ci.yml` | The thin `ci.yml` a service repo drops in to call the reusable workflow. |
| `infra-ci.yml` | Validates this repo's Terraform/Terragrunt on PR: `fmt -check`, per-module `validate`, `tflint`, read-only `terragrunt plan`. **Never applies.** |
| `schema-validate.yml` | Pointer-only stub. The real schema CI runs in the **contracts** repo. |

> Authoritative spec: `archive/Manoj/phases/phase-01-infrastructure.md` §Component 18.
> Cross-referenced: `phase-00-contracts.md` Contract 5 (Kafka), Contract 14 (migrations).

---

## 1. Auth model — GitHub OIDC, no long-lived keys

There are **zero long-lived AWS access keys** anywhere in CI. Every AWS call is
made under a short-lived session obtained via GitHub OIDC.

```
GitHub Actions job  --(OIDC id-token)-->  sts:AssumeRoleWithWebIdentity
                                              │
                                              ▼
                              cypherx-<env>-github-actions   (the GitHubActionsRole)
                              defined in modules/iam (Component 1)
```

- The role ARN is `arn:aws:iam::<account-id>:role/cypherx-<env>-github-actions`.
- Trust policy (see `modules/iam/main.tf`) allows
  `sts:AssumeRoleWithWebIdentity` from `token.actions.githubusercontent.com`,
  `aud = sts.amazonaws.com`, and `sub` matching `repo:cypherx-ai/*:...`
  (`env.hcl: github_oidc_subjects`).
- Workflows request the token with `permissions: id-token: write`. The caller
  workflow MUST also grant `id-token: write` for `workflow_call` to inherit it.

**What the GitHubActionsRole can do (least privilege, Component 1 + 18):**

| Allowed | Scope |
|---------|-------|
| `ecr:GetAuthorizationToken` | `*` (required global) |
| ECR push/pull verbs (`PutImage`, `*LayerUpload`, `BatchGetImage`, …) | `repository/cypherx/*` only |
| S3 state **read** (`GetObject`, `ListBucket`) | the terraform-state bucket only |
| `eks:DescribeCluster`, `eks:ListClusters` | `cluster/cypherx-*` only |
| `secretsmanager:GetSecretValue` | `secret:cypherx/ci/*` only |

**What it can NEVER do:** any IAM action — there is an explicit `Deny iam:*` and
`Deny sts:AssumeRole` on the role. CI cannot create roles, cannot privilege-
escalate, cannot assume the Terraform roles. (Terraform `apply` is done
out-of-band under `TerraformInfraRole` / `TerraformIAMRole` by an operator, not
by CI — `infra-ci.yml` only `plan`s, read-only.)

---

## 2. CI secret fetch — Secrets Manager `cypherx/ci/*`

Doppler is reserved for **in-cluster pods** (operator-synced env vars). CI
workflows that need a bootstrap secret read it from **AWS Secrets Manager** under
the `cypherx/ci/<workflow>/` prefix — the only path by which GitHub Actions
reaches a per-workflow secret. There are **no Doppler API tokens in GitHub
Secrets** and no long-lived anything in GitHub.

```
aws secretsmanager get-secret-value --secret-id cypherx/ci/<workflow>/<key>
        │   (allowed by GitHubActionsRole; resource = secret:cypherx/ci/*)
        ▼
short-lived plaintext, masked in logs, used within the job, never persisted
```

Conventions:

- Prefix: `cypherx/ci/<workflow>/<key>` (e.g. `cypherx/ci/gitops-bot/app_id`).
- The GitHubActionsRole's `secretsmanager:GetSecretValue` is scoped to
  `arn:aws:secretsmanager:<region>:<acct>:secret:cypherx/ci/*` and nothing else.
- Secrets that originate in Doppler (like the gitops App private key) are
  **mirrored** into `cypherx/ci/*` for CI consumption — Doppler remains the
  system of record and rotation source (see §4).

---

## 3. Image tag convention (locked — deployments depend on it)

Set in `reusable-service-ci.yml`. ECR repos are created `IMMUTABLE` (see
`environments/_envcommon/ecr.hcl`) so these tags can never be overwritten.

| Trigger | Tag | Lifetime |
|---------|-----|----------|
| `pull_request` | `cypherx/<service>:pr-<pr-number>-<git-sha7>` | lifecycle-expired after merge/close (ECR keeps last N pr-* images) |
| push to `main` | `cypherx/<service>:sha-<git-sha7>` | **immutable, kept forever** |
| push of `v*` tag | `cypherx/<service>:<semver>` (e.g. `v1.2.3`) | **kept forever** (signed in Phase 13 hardening) |

**`:latest` is forbidden** in any image push or deployment manifest. The pipeline
hard-fails if a computed tag resolves to `latest`. The gitops PR always writes an
immutable `sha-<sha7>` tag — never a moving tag — so an ArgoCD sync is fully
reproducible.

The registry host is `<account-id>.dkr.ecr.<region>.amazonaws.com` and the repo
is `cypherx/<service>` (the Component 5 list of 13 services).

---

## 4. GitOps cross-repo PR — the cypherx-gitops-bot token exchange

On merge to `main`, the pipeline opens a PR against the **gitops** repo bumping
the service's image tag. It authenticates as the **`cypherx-gitops-bot` GitHub
App** — never a personal PAT (a PAT ties every deployment to one human and
breaks when they leave).

```
Doppler  ci/github_app_private_key   (system of record, 180-day rotation)
   │  mirrored to ──►  Secrets Manager  cypherx/ci/gitops-bot/private_key
   │                                     cypherx/ci/gitops-bot/app_id
   ▼
CI job (OIDC role) reads cypherx/ci/gitops-bot/*  ──►  App private key (PEM, masked)
   │
   ▼  actions/create-github-app-token   (JWT -> installation token exchange)
short-lived INSTALLATION TOKEN  (contents:write + pull_requests:write on gitops repo only)
   │
   ▼  git push branch + gh pr create  ──►  gitops PR
```

- **App:** `cypherx-gitops-bot`. Installed on service repos (read) and the
  `cypherx-gitops` repo (`contents:write`, `pull_requests:write`).
- **Private key:** lives in **Doppler at `ci/github_app_private_key`**, rotated
  **every 180 days** (Component 18). It is mirrored into Secrets Manager
  (`cypherx/ci/gitops-bot/private_key`) so CI reaches it via the OIDC role's
  scoped `cypherx/ci/*` read — CI never holds a Doppler API token.
- **Token lifetime:** the installation token is short-lived (≈1h) and scoped to
  the single gitops repo. The pipeline mints a fresh one per run.
- **Merge policy:** dev/staging gitops PRs are **auto-merged** (ArgoCD then syncs
  automatically). **Prod requires human approval** — enforced in ArgoCD
  (Component 12), not in CI. The bot opens the prod PR but does not merge it.

### Rotation runbook (180 days)

1. Generate a new private key in the `cypherx-gitops-bot` App settings; keep the
   old one active (GitHub Apps allow multiple keys).
2. Write the new PEM to Doppler `ci/github_app_private_key` (all envs).
3. Mirror to Secrets Manager `cypherx/ci/gitops-bot/private_key`.
4. Trigger one dummy gitops PR to confirm the new key works end-to-end.
5. Delete the old key from the App settings.
6. Record operator + timestamp in the env infra changelog.

---

## 5. Pipeline stages (reusable-service-ci.yml)

```
lint ──► unit-test ──► build-scan-push ──► gitops-pr (main only)
 │          │               │  │  │              │
 │          │               │  │  └ push ECR (OIDC, immutable tag)
 │          │               │  └ Trivy scan — FAIL on CRITICAL CVEs
 │          │               └ docker build (multi-stage, cached)
 │          └ go test / npm test / pytest
 └ golangci-lint / eslint / ruff   (by `language` input)
```

- **Trivy gate:** `exit-code: 1`, `severity: CRITICAL`, `ignore-unfixed: true`.
  A CRITICAL CVE with a fix available fails the build. ECR `scan_on_push` is
  defence-in-depth; Trivy in CI is the blocking gate.
- **Build then scan then push:** we scan the exact artifact we would push; push
  only happens after the scan passes and only on push/PR/tag events.

---

## 6. infra-ci.yml — IaC validation (this repo)

- `terraform fmt -check -recursive` over the whole repo (2-space, aligned `=`).
- `terraform validate` + `tflint` per leaf module/addon dir (`init -backend=false`,
  no AWS creds needed).
- `terragrunt plan` (read-only) on PR for the `vpc`, `dns`, `ecr` dev stacks,
  posted to the job summary. **No `apply` ever runs in CI.**

Apply is a deliberate, human-gated, role-assumed action:
`TerraformInfraRole` for infra stacks, `TerraformIAMRole` for `environments/*/iam/`
(which additionally requires a CODEOWNERS second approver — see
`modules/iam/README.md`).

---

## 7. Contracts schema CI lives elsewhere

`schema-validate.yml` here is a **pointer stub**. JSON-Schema/OpenAPI/YAML
validation runs in the **contracts** repo
(`contracts/.github/workflows/contracts-ci.yml`, mirrored in `.gitlab-ci.yml`):
`npm run check` → `npm run validate` (Ajv draft 2020-12) → `npm run lint:openapi`
(redocly, Contract 10). Do not duplicate it here.
