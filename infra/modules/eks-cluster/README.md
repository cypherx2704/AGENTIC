# `eks-cluster` module — Component 4

Provisions one EKS cluster per environment (`cypherx-<env>`) with a **private-only**
API server, OIDC/IRSA, control-plane logging to CloudWatch, the three AWS-managed
add-ons, and exactly two static managed node groups.

Wraps [`terraform-aws-modules/eks/aws`](https://registry.terraform.io/modules/terraform-aws-modules/eks/aws) `~> 20`.

> Spec: `archive/Manoj/phases/phase-01-infrastructure.md` Component 4 (lines 196-250).

## Locked-in design guards (do NOT "fix")

- **API server is PRIVATE ONLY.** `endpoint_public_access = false`. Developers reach
  the API via AWS SSO + VPN; CI uses **self-hosted GitHub runners inside the VPC** with
  an IRSA role. GitHub-hosted-runner IP allow-listing is **FORBIDDEN** (those ranges churn).
- **Managed node groups vs Karpenter non-overlap.** This module creates only
  `system-nodes` and `observability`. The `core`, `agent`, and `tools` node-roles are
  owned by **Karpenter NodePools** (G5 / Component 17b). Do NOT add managed node groups
  for those roles — the two scalers fight (managed-NG ASG adds a node, Karpenter
  consolidates it minutes later, repeat).
- **`system-nodes` taint** `CriticalAddonsOnly=true:NoSchedule` keeps non-system pods off
  the nodes that host `kube-system` + Karpenter itself.
- **`observability` nodes** host pinned Prometheus/Loki EBS PVCs. Karpenter must not
  consolidate them (enforced by the Karpenter disruption budget in G5, not this module).

## What it creates

| Resource | Detail |
|----------|--------|
| EKS cluster | K8s `1.30`, private API, secrets envelope-encrypted with KMS |
| Control-plane logs | `api`, `audit`, `authenticator` -> CloudWatch (`90d` retention default) |
| OIDC provider | enabled (`enable_irsa = true`) for per-service IRSA in later phases |
| Managed add-ons | `kube-proxy`, `vpc-cni` (`before_compute`), `coredns` |
| `system-nodes` NG | `t3.medium` x3, `ON_DEMAND`, label `node-role=system`, taint `CriticalAddonsOnly` |
| `observability` NG | `m5.large` x2, `ON_DEMAND`, label `node-role=observability` |
| KMS key | dedicated key for secrets encryption (unless `kms_key_arn` supplied) |

## Key inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` -> cluster `cypherx-<env>` |
| `kubernetes_version` | `1.30` | control-plane version |
| `vpc_id` | — | from the `vpc` module (G1) |
| `private_subnet_ids` | — | 3 AZ private subnets for control plane + nodes |
| `cluster_security_group_ids` | `[]` | extra SGs on control-plane ENIs |
| `node_security_group_ids` | `[]` | extra SGs on nodes (e.g. `sg-eks-nodes`) |
| `endpoint_public_access` | `false` | **keep false** |
| `enabled_cluster_log_types` | `["api","audit","authenticator"]` | |
| `system_node_group` | `t3.medium` x3 | object: instance_types/size/disk |
| `observability_node_group` | `m5.large` x2 | object: instance_types/size/disk |
| `kms_key_arn` | `null` | reuse an existing key, else one is created |

## Key outputs

| Name | Notes |
|------|-------|
| `cluster_name` / `cluster_arn` / `cluster_endpoint` | cluster identity + private API endpoint |
| `cluster_certificate_authority_data` | for kubeconfig |
| `oidc_provider_arn` / `cluster_oidc_issuer_url` | IRSA wiring for Phase 2+ service roles |
| `cluster_security_group_id` / `node_security_group_id` | for cross-module SG rules |
| `eks_managed_node_groups` | node group attributes |

## Environment sizing

`dev` uses the defaults (`t3.medium`/`m5.large`). `prod` keeps the same instance families
per Component 4 (the node-group sizes are fixed in the spec — only the Karpenter-owned
NodePools grow). Override `system_node_group` / `observability_node_group` from the
Terragrunt `inputs` block if an environment needs larger static nodes.

> Secrets (none here) and AWS credentials are sourced from the CI role / Doppler — no
> hardcoded values in this module.
