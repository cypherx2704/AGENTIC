# nodered-tenant — per-tenant Node-RED prerequisites (production)

The Flow-Tool-Bridge's `KubernetesProvisioner` creates one hardened Node-RED instance per
tenant **at runtime** (a `Deployment` + `PersistentVolumeClaim` + `Service` + egress-deny
`NetworkPolicy`, rendered in `services/provisioner.py`). This directory holds the cluster
prerequisites that must exist **before** the bridge can do that:

| Manifest | Purpose |
|----------|---------|
| `manifests/namespace.yaml` | The `cypherx-tools` namespace the tenant instances live in. |
| `manifests/provisioner-rbac.yaml` | A `Role` + `RoleBinding` granting the bridge's ServiceAccount permission to create/replace Deployments, Services, PVCs and NetworkPolicies in `cypherx-tools`. |
| `manifests/nodered-shared-secrets.example.yaml` | Template for the `nodered-shared-secrets` Secret (admin token, invoke secret, credential secret) shared by every tenant Node-RED and the bridge. In production this is a Doppler-synced `DopplerSecret`, not a committed Secret. |
| `manifests/baseline-default-deny.yaml` | A namespace default-deny `NetworkPolicy` so any pod without an explicit allow (belt-and-braces with the per-instance policy the provisioner creates). |

## Isolation model
- One Node-RED per tenant (no shared workspace). Non-root, dropped caps, resource limits,
  optional `runtimeClassName: gvisor` (set via the bridge's `NODERED_RUNTIME_CLASS`).
- The per-instance `NetworkPolicy` (created by the provisioner) allows ingress only from the
  bridge and egress only to DNS + an explicit CIDR allow-list (`NODERED_EGRESS_ALLOW_CIDRS`) —
  it can NEVER reach internal platform services or other tenants.
- The admin/invoke/credential secrets are platform-wide (the bridge is the sole trusted caller
  and the NetworkPolicy isolates each instance); per-tenant secret rotation is a follow-up.

Apply order: `namespace` → `nodered-shared-secrets` (or DopplerSecret) → `provisioner-rbac`
→ `baseline-default-deny`, then deploy `charts/tool-flow-bridge` with `PROVISIONER_MODE=kubernetes`.
