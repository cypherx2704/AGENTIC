# Phase 1 — Infrastructure Notes (G10 cross-phase reconciliation)

Operator-facing summary of what the Phase 1 IaC in this repo **authors** versus what a human
**must still do** to stand up an environment, plus the canonical **apply order**.

Authoritative spec: `archive/Manoj/phases/phase-01-infrastructure.md`.
Phase 0 contracts referenced: `contracts/` (Contract 1 OIDC, Contract 5 Kafka, Contract 12/14).

---

## 1. What is authored in this repo (code, not running infra)

All of the following is committed IaC — Terraform modules, Terragrunt stacks, Terraform-managed
Helm releases, the base Helm chart, and the smoke test. None of it is *applied*; it produces no
AWS resources until an operator runs `terragrunt apply` with credentials.

- **Terraform modules** (`modules/`): `tfstate-backend`, `vpc`, `eks-cluster`, `kafka`,
  `postgresql`, `valkey`, `ecr-repo`, `s3-bucket`, `dns`, `iam`, `postgres-bootstrap`,
  `kafka-topics`, `doppler-bootstrap`. Each has `main.tf`/`variables.tf`/`outputs.tf`/`versions.tf`/`README.md`,
  pins Terraform `>= 1.9` and AWS provider `~> 5.x`, uses official `terraform-aws-modules` where sensible.
- **Terragrunt environments** (`environments/`): root `terragrunt.hcl` wires the S3 + DynamoDB
  remote state and generates the provider block; `dev/`, `staging/`, `prod/` with per-stack
  `terragrunt.hcl` + `env.hcl` sizing. Dev = small / single-AZ; prod = large / multi-AZ
  (RDS `db.r6g.xlarge` multi-AZ, Valkey 3-node, MSK `kafka.m5.large`).
- **K8s add-ons** (`k8s-addons/`): Terraform-managed, version-pinned Helm releases for Istio,
  Kong, ArgoCD, cert-manager, AWS LBC, kube-prometheus-stack, Loki, Tempo, Promtail,
  Doppler operator, metrics-server, Karpenter (NodePool + EC2NodeClass), external-dns, reloader,
  plus namespaces and network-policies.
- **Base Helm chart** (`../charts/cypherx-service`, owned by G6): contract-enforcing service chart.
- **Local dev** (`dev/local/`): Tilt + kind + Docker Compose (Redpanda/MinIO) SharedCore loop.
- **Smoke test** (`smoketest/echo-service` + `scripts/infra-smoke-test.sh`): Component 21 gate.

### Reconciled invariants honoured (do NOT "fix" these)

- **ALB → Kong is plaintext** inside the VPC (SG-scoped); Kong → backends is Istio mTLS.
- **`/v1/agents/*`, `/v1/tokens/*`, `/v1/authorize`, `/v1/service-tokens` route to Auth**, not xAgent.
- **Managed node groups (system-nodes, observability) and Karpenter NodePools (core, agent, tools)
  do not overlap** — never create a managed NG for a Karpenter-owned role.
- **Compact `auth.agent.*` topics key on `agent_id`**, not `tenant_id` (see `contracts/kafka/topics.md` §4.1).
- **`CREATEROLE` is cluster-wide**, not schema-scoped — mitigated via per-service DDL user + isolated Doppler secret.
- **`iss` is an opaque identifier; JWKS URL is discovered per-env** at
  `https://auth.<env>.cypherx.ai/.well-known/jwks.json` (see `contracts/jwt/oidc-discovery.md` §6).

---

## 2. What an operator must still do (human / out-of-band)

This IaC does **not** self-apply. Before Phase 2, a platform operator must:

1. **AWS credentials + account.** Have the `cypherx-ai` account; assume `TerraformInfraRole`
   (infra stacks) and `TerraformIAMRole` (the `iam` stack — second-approver gated). No long-lived keys.
2. **Bootstrap remote state once.** Create the S3 state bucket + DynamoDB lock table from the
   `tfstate-backend` module (chicken-and-egg: this stack uses local state for its own apply, then
   the bucket it created backs everything else).
3. **DNS delegation.** Delegate `cypherx.ai` NS records from the registrar to the Route53 hosted
   zone before the `dns`/ACM stack can validate certs. ACM DNS validation will hang until delegated.
4. **Doppler human bootstrap (one-time per env).** Run `environments/<env>/doppler-bootstrap/`
   with a **personal `DOPPLER_TOKEN`** in the operator's shell. That apply creates per-env service
   tokens and writes the long-lived Terraform token to Doppler `ci/doppler_api_token`. **Revoke the
   personal token immediately after**, and record operator name + timestamp in the env changelog.
   This is the only step where a human-held secret touches an environment.
5. **Populate Doppler secret paths** (dev + staging before Phase 2): `service-auth/<svc>/bootstrap_secret`,
   `db/<svc>/runtime_password`, `db/<svc>/ddl_password`, `ci/github_app_private_key`.
6. **Install the `cypherx-gitops-bot` GitHub App** and register the GitOps repo + deploy key in ArgoCD.
7. **Stand up self-hosted GitHub runners inside the VPC** (EKS API is private-only; no public endpoint).
8. **Run the Component 21 smoke test** and require it to pass **two consecutive runs** before
   marking Phase 1 complete.

---

## 3. Apply order (canonical)

Run per environment (`dev` first, then `staging`, then `prod`). Each step depends on outputs of
the previous one (VPC IDs, subnet IDs, EKS OIDC/IRSA, RDS/MSK endpoints, kubeconfig).

```
1.  tfstate-backend       S3 state bucket + DynamoDB lock table (bootstrap; local state for itself)
2.  iam                   OIDC provider, TerraformInfra/IAM roles, IRSA base, GitHubActionsRole
3.  vpc                   VPC 10.0.0.0/16, 3 private + 3 public subnets, NAT/AZ, SGs, IGW
4.  eks                   cypherx-<env> cluster (private API), managed NGs, OIDC for IRSA
5.  data (rds / valkey / kafka)   RDS PostgreSQL 16, Valkey 7.x, MSK 3.6.x  (+ ecr, dns alongside)
6.  postgres-bootstrap    CREATE DATABASE/schemas/runtime+DDL users, pgvector, grants (Terraform-owned)
7.  kafka-topics          declarative MSK topics (Component 17): partitions/replication/cleanup/retention + DLQ
8.  k8s-addons            Helm: namespaces → Istio → Kong → cert-manager → AWS LBC → metrics-server →
                          Karpenter → external-dns → reloader → ArgoCD → Doppler operator →
                          kube-prometheus-stack → Loki → Tempo → Promtail
9.  doppler-bootstrap     per-env service tokens + write ci/doppler_api_token back to Doppler (human-run first time)
10. smoke test            scripts/infra-smoke-test.sh — 10 assertions, MUST pass 2 consecutive runs
```

Notes:
- Step 5 stacks (`rds`/`valkey`/`kafka`, plus `ecr` and `dns`) are independent of each other and
  may apply in parallel once `vpc` + `iam` exist; they all depend on subnets and SGs.
- `dns`/ACM must wait on registrar NS delegation (§2.3) before cert validation succeeds.
- `postgres-bootstrap` (step 6) and `kafka-topics` (step 7) need the RDS and MSK endpoints from
  step 5 and the SGs that allow EKS-node access.
- `doppler-bootstrap` (step 9) is human-run on the very first apply per env, then CI-run thereafter.

---

## 4. Cross-phase reconciliation deltas applied (G10)

- `contracts/kafka/topics.md`: added the **mandatory compact-topic `agent_id` message-key rule**
  (§4.1) and the **Phase 1 operational topics** from Component 17 (`cypherx.auth.agent.deactivated`,
  `cypherx.llms.budget.alert`, `cypherx.agent.task.submitted`, `cypherx.platform.audit.event`,
  `cypherx.billing.usage.recorded`) with partitions / replication / cleanup.policy / retention / DLQ.
- `contracts/jwt/oidc-discovery.md`: added the **`iss` (opaque) vs per-env JWKS URL** cross-reference
  (Component 5) — verifiers configure JWKS URL per env and do NOT derive it from `iss`.
- `charts/README.md` left to **G6** (already authored — not overwritten by G10).
