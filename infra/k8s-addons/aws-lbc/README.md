# k8s-addons/aws-lbc — AWS Load Balancer Controller (Component 10)

Terraform-managed Helm release for the AWS Load Balancer Controller.

- **Chart:** `eks/aws-load-balancer-controller` (`var.chart_version`, default `1.8.1`)
- **Namespace:** `kube-system`
- **Purpose:** watches K8s `Service type:LoadBalancer` / `Ingress` → creates AWS
  ALBs automatically. The Kong proxy Service (Component 8) is the primary
  consumer.

## IRSA

The controller ServiceAccount (`var.service_account_name`,
default `aws-load-balancer-controller`) is annotated with
`eks.amazonaws.com/role-arn = var.irsa_role_arn`. That role —
`AWSLoadBalancerControllerRole` — is created in **`modules/iam`** (another
group) and passed in here. Its policy grants `ec2:*`,
`elasticloadbalancing:*`, `iam:CreateServiceLinkedRole` (Component 10).

This module does **not** create IAM resources (separation of duty — IAM lives in
`environments/<env>/iam/` via `TerraformIAMRole`).

## Scheduling

Pinned to the `system` managed node group (`node-role=system`) with a
`CriticalAddonsOnly` toleration, matching Component 4's taint on system nodes.

## Inputs

| Variable | Default | Notes |
|----------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `chart_version` | `1.8.1` | Pinned |
| `namespace` | `kube-system` | |
| `cluster_name` | — | `cypherx-<env>` (from eks stack) |
| `region` | `us-east-1` | |
| `vpc_id` | — | from vpc stack |
| `irsa_role_arn` | — | `AWSLoadBalancerControllerRole` from `modules/iam` |
| `service_account_name` | `aws-load-balancer-controller` | Must match IRSA trust |

## Secrets

None. Auth is via IRSA — no static AWS credentials.
