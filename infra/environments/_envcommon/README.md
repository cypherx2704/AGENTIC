# `_envcommon` — shared Terragrunt include fragments

These files are **not** standalone stacks. Each one is `read_terragrunt_config(...)`-included by the matching
`environments/<env>/<stack>/terragrunt.hcl` and supplies the env-invariant inputs + the `terraform { source = ... }`
pointer for that stack. Per-env varying values (sizes, AZ count, multi-AZ flags) come from `environments/<env>/env.hcl`
and are merged on top.

Pattern in a leaf stack:

```hcl
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/vpc.hcl"
  expose = true
}

inputs = {
  # only the handful of values that differ from the envcommon defaults, pulled from env.hcl
}
```

This keeps `staging/` and `prod/` thin: they are mostly just an `env.hcl` plus near-empty stack files that
include the same `_envcommon` fragment as `dev/`.

## Fragments

| File              | Stack          | Module source                  | Component(s) |
|-------------------|----------------|--------------------------------|--------------|
| `vpc.hcl`         | vpc            | `modules/vpc`                  | 3            |
| `eks.hcl`         | eks            | `modules/eks-cluster`          | 4            |
| `kafka.hcl`       | kafka          | `modules/kafka`                | 5            |
| `postgresql.hcl`  | postgresql     | `modules/postgresql`           | 5            |
| `valkey.hcl`      | valkey         | `modules/valkey`               | 5            |
| `ecr.hcl`         | ecr            | `modules/ecr-repo`             | 5            |
| `dns.hcl`         | dns            | `modules/dns`                  | 5            |
| `iam.hcl`         | iam            | `modules/iam`                  | 1            |

> The `vpc`, `eks-cluster`, `kafka`, `postgresql`, `valkey`, `ecr-repo`, `dns`, and `iam` modules are owned by other
> groups (G1/G2). Group G3 owns the **wiring** (these fragments + the env stack files) plus the three bootstrap
> modules (`postgres-bootstrap`, `kafka-topics`, `doppler-bootstrap`) which have their own dedicated stack dirs.
