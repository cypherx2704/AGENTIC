# k8s-addons/karpenter — Components 4 & 17b

Installs Karpenter (`v1.x`, OCI chart) and the **NodePool + EC2NodeClass** CRDs
(NOT the deprecated `Provisioner`).

## NodePools (Component 4)

| NodePool | Instance shape           | Capacity            | node-role label | Workload |
|----------|--------------------------|---------------------|-----------------|----------|
| `core`   | `c5.xlarge` family       | **on-demand only**  | `core`          | shared-core, ingress |
| `agent`  | `c5.2xlarge` / `c6i`     | **on-demand + spot**| `agent`         | xagent, orchestrator (HPA-driven) |
| `tools`  | `c5.large` / `c6i`       | **on-demand + spot**| `tools`         | tools/* (Phase 7+) |

All three share one **`cypherx-compute` EC2NodeClass** (AL2023 EKS-optimized AMI,
gp3 root, IMDSv2-only, subnet/SG discovery via the `karpenter.sh/discovery` tag).

## Non-overlap guard (Component 4 — do NOT "fix" by adding NodePools)

There is **deliberately no `observability` NodePool and no `system` NodePool**:

- **`observability`** is a fixed EKS-managed node group. Prometheus/Loki/Tempo
  PVCs are pinned and the doc says *"do NOT consolidate observability"* —
  Karpenter must never touch it (consolidation breaks EBS attach).
- **`system`** is a fixed EKS-managed node group hosting kube-system **and
  Karpenter itself**. Karpenter cannot provision its own host node.

Creating a NodePool for either role would overlap the managed NG; the managed-NG
ASG would add a node and Karpenter would consolidate it minutes later, repeatedly.
The guard is enforced simply by never declaring those NodePools (and is asserted
by the `managed_nodegroup_roles_excluded` output).

## Controller placement

The controller runs in `kube-system`, pinned to `node-role=system` with a
`CriticalAddonsOnly` toleration — it lives on the managed NG, not on the nodes it
provisions. IRSA role + interruption queue come from the G3 IAM/EKS stacks.

## Disruption / consolidation

`core`/`agent`/`tools` use `WhenEmptyOrUnderutilized` consolidation (stateless,
HPA-driven). `expireAfter=720h` rotates nodes for AMI/patch freshness. Spot is
restricted to `agent` + `tools` only.

## Inputs (highlights)

| Variable                | Notes |
|-------------------------|-------|
| `cluster_name`          | `cypherx-<env>`. |
| `cluster_endpoint`      | EKS API endpoint. |
| `chart_version`         | `1.0.6` (Karpenter v1.x). |
| `controller_role_arn`   | IRSA, from G3 IAM. No static keys. |
| `node_iam_role_name`    | EKS node role (ECR-pull scoped). |
| `discovery_tag_value`   | `karpenter.sh/discovery` tag value (defaults to cluster name). |
| `ami_alias`             | `al2023@latest` (matches cluster K8s 1.30). |

> CRDs (`NodePool`, `EC2NodeClass`) are installed by the Karpenter chart in the
> same apply; the CR manifests use `gavinbunney/kubectl` (`kubectl_manifest`) so
> they tolerate the apply-time-unknown CRD better than `kubernetes_manifest`.
