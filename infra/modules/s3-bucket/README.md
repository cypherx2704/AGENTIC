# Module: `s3-bucket`

Reusable **encrypted S3 bucket**. Primary consumers are the observability log/trace
object stores in Component 13:

- `cypherx-loki-logs-<env>` — Loki chunk store (retention **30 days**).
- `cypherx-tempo-traces-<env>` — Tempo trace store (retention **7 days**).

## Guarantees

- **SSE encryption** — `aws:kms` by default (AWS-managed `aws/s3` key unless a
  CMK ARN is given); `AES256` selectable.
- **Public access blocked** — all four block flags set.
- **TLS enforced** — bucket policy denies `aws:SecureTransport = false`.
- **Lifecycle** — optional current-object expiration (`expiration_days`) to match
  the consumer's retention window, plus incomplete-multipart cleanup. Noncurrent
  versions expire when versioning is enabled.

## Example (observability stack)

```hcl
module "loki_bucket" {
  source          = "../../modules/s3-bucket"
  bucket_name     = "cypherx-loki-logs-${var.env}"
  expiration_days = 30
  tags            = local.tags
}

module "tempo_bucket" {
  source          = "../../modules/s3-bucket"
  bucket_name     = "cypherx-tempo-traces-${var.env}"
  expiration_days = 7
  tags            = local.tags
}
```

The bucket name is passed to the Loki/Tempo Helm releases in the
`k8s-addons/loki` and `k8s-addons/tempo` stacks (owned by another group).

## Inputs (highlights)

| Name | Default | Notes |
|------|---------|-------|
| `bucket_name` | — | |
| `versioning_enabled` | `false` | log/trace stores are immutable |
| `sse_algorithm` | `aws:kms` | |
| `expiration_days` | `0` (off) | set to retention (Loki 30, Tempo 7) |
| `force_destroy` | `false` | |

## Outputs

`bucket_id`, `bucket_arn`, `bucket_domain_name`.
