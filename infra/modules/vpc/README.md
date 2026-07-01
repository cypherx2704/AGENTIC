# Module: `vpc`

Component 3 — **VPC & Networking**.

## Network layout (Component 3, verbatim)

```
VPC: 10.0.0.0/16   (us-east-1)

Private (EKS nodes, RDS, MSK, ElastiCache):
  10.0.1.0/24  (us-east-1a)
  10.0.2.0/24  (us-east-1b)
  10.0.3.0/24  (us-east-1c)

Public (ALB, NAT gateways only):
  10.0.101.0/24 (us-east-1a)
  10.0.102.0/24 (us-east-1b)
  10.0.103.0/24 (us-east-1c)

NAT Gateways: 1 per AZ (3 total — HA)   [dev may set single_nat_gateway=true]
Internet Gateway: 1
Route tables: public -> IGW; private -> per-AZ NAT
```

Networking is built on `terraform-aws-modules/vpc/aws ~> 5.8`. Subnets are tagged
for EKS (`kubernetes.io/cluster/<cluster>`) and the AWS Load Balancer Controller
(`kubernetes.io/role/elb` on public, `internal-elb` on private).

## Security groups (the six from Component 3)

| SG | Inbound | Outbound |
|----|---------|----------|
| `sg-alb` | 443 from `0.0.0.0/0` | to `sg-kong` (8000) |
| `sg-kong` | from `sg-alb` (8000) | to `sg-eks-nodes` |
| `sg-eks-nodes` | from `sg-kong` + self | 443 (AWS APIs) + intra-VPC data plane + DNS |
| `sg-rds` | 5432 from `sg-eks-nodes` | — |
| `sg-valkey` | 6379 from `sg-eks-nodes` | — |
| `sg-kafka` | 9092, 9094 from `sg-eks-nodes` | — |

### Intentional boundary (do NOT change)

`sg-alb -> sg-kong` is **plain HTTP on 8000** inside the VPC. This is the
deliberate ALB→Kong plaintext boundary from Component 8: `sg-kong` only accepts
traffic from `sg-alb`, the path stays inside AWS-managed VPC infra, and
Kong→backend is mTLS via Istio. A reviewer who "fixes" this to HTTPS breaks the
deploy.

Private data services (`sg-rds`, `sg-valkey`, `sg-kafka`) accept traffic **only**
from `sg-eks-nodes` — there is no `0.0.0.0/0` ingress on any private service
(satisfies the health-checklist item "no 0.0.0.0/0 on private services").

## Inputs (highlights)

| Name | Default | Notes |
|------|---------|-------|
| `vpc_cidr` | `10.0.0.0/16` | |
| `azs` | `[us-east-1a,b,c]` | 3 AZs |
| `private_subnet_cidrs` | `10.0.1-3.0/24` | |
| `public_subnet_cidrs` | `10.0.101-103.0/24` | |
| `single_nat_gateway` | `false` | dev=true (cost), prod=false (HA, 3 NAT GWs) |
| `cluster_name` | — | for subnet/cluster tagging |

## Outputs

`vpc_id`, `vpc_cidr`, `private_subnet_ids`, `public_subnet_ids`, `nat_gateway_ids`,
`azs`, and the six SG IDs (`sg_alb_id`, `sg_kong_id`, `sg_eks_nodes_id`,
`sg_rds_id`, `sg_valkey_id`, `sg_kafka_id`).

The data-layer modules (rds, valkey, kafka) consume `sg_rds_id` / `sg_valkey_id`
/ `sg_kafka_id`; the eks module consumes `sg_eks_nodes_id` and the subnet IDs.
