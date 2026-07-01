# k8s-addons/external-dns — external-dns (Component 17b)

Terraform-managed Helm release for external-dns with the AWS **Route53** provider.

- **Chart:** `external-dns/external-dns` (`var.chart_version`, default `1.14.5`)
- **Namespace:** `kube-system`
- **Provider:** AWS Route53, scoped to the `cypherx.ai` hosted zone
- **Sources:** `ingress` **and** `service` annotations (Component 17b)

## What it does

Watches K8s Ingress/Service annotations and creates matching Route53 records
(e.g. `api.<env>.cypherx.ai`, `auth.<env>.cypherx.ai`, `grafana.<env>...`).
Without it, every new ingress hostname needs a manual Route53 entry (Component
5/17b).

## IRSA

The ServiceAccount (`var.service_account_name`, default `external-dns`) is
annotated with `eks.amazonaws.com/role-arn = var.irsa_role_arn`. That role —
the ExternalDNS Route53 role — is created in **`modules/iam`** and passed in. It
grants `route53:ChangeResourceRecordSets` on the `cypherx.ai` zone plus
`ListHostedZones` / `ListResourceRecordSets`.

## Record ownership

`policy = sync`, `registry = txt`, `txtOwnerId = cypherx-<env>` (override via
`var.txt_owner_id`). The TXT registry stops different envs/clusters from
clobbering each other's records in the shared `cypherx.ai` zone. `zoneIdFilters`
is pinned to `var.route53_zone_id` from the dns stack.

## Inputs

| Variable | Default | Notes |
|----------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `chart_version` | `1.14.5` | Pinned |
| `namespace` | `kube-system` | |
| `irsa_role_arn` | — | ExternalDNS role from `modules/iam` |
| `service_account_name` | `external-dns` | Must match IRSA trust |
| `domain_filter` | `cypherx.ai` | |
| `route53_zone_id` | — | from dns stack |
| `txt_owner_id` | `cypherx-<env>` | Registry ownership |

## Secrets

None. Auth is via IRSA — no static AWS credentials.
