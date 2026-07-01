# Module: `ecr-repo`

Component 5 — reusable **ECR repository** (one instance per service image).

## Behaviour

- **Scan-on-push** enabled (`scan_on_push = true`).
- **Immutable tags** by default — matches the Component 18 tagging convention
  (`sha-<git-sha7>`, `<semver>`, `pr-<n>-<sha7>`); `:latest` is never used.
- **KMS** encryption by default (AWS-managed ECR key unless `kms_key_arn` given).
- Lifecycle policy:
  1. **Expire untagged images after 14 days** (`untagged_expire_days`).
  2. **Keep only the last N tagged images** (`keep_last_tagged`, default 30) for
     the `sha-`, `v`, `pr-` tag prefixes.

## Usage (driven from `environments/<env>/ecr/terragrunt.hcl`)

The ECR stack loops this module over the Component 5 repository list:

```
cypherx/auth-service        cypherx/orchestrator
cypherx/llms-gateway        cypherx/platform-management
cypherx/guardrails-service  cypherx/tool-web-search
cypherx/memory-service      cypherx/tool-code-exec
cypherx/rag-service         cypherx/tool-http-client
cypherx/xagent              cypherx/tool-file-ops
                            cypherx/px0-bridge
```

(Later phases add `cypherx/skills-service` (Phase 8), `cypherx/a2a-service`
(Phase 10), `cypherx/web-frontend` (Phase 12) — not at Phase 1.)

Pull access for EKS nodes comes from the node role's
`AmazonEC2ContainerRegistryReadOnly` attachment (see `modules/iam`); push access
comes from `GitHubActionsRole`. `pull_principal_arns` is only needed for extra
cross-account principals.

## Inputs (highlights)

| Name | Default | Notes |
|------|---------|-------|
| `name` | — | e.g. `cypherx/auth-service` |
| `image_tag_mutability` | `IMMUTABLE` | |
| `scan_on_push` | `true` | |
| `untagged_expire_days` | `14` | Component 5 |
| `keep_last_tagged` | `30` | "keep last N" |
| `tag_prefixes_to_keep` | `["sha-","v","pr-"]` | Component 18 tags |

## Outputs

`repository_url`, `repository_arn`, `repository_name`, `registry_id`.
