# Module: `tfstate-backend`

Component 2 — **Terraform Remote State**.

Provisions the S3 + DynamoDB backend that every other stack in this repo uses for
remote state and locking.

## Resources

| Resource | Configuration |
|----------|---------------|
| `aws_s3_bucket.state` | `cypherx-terraform-state-<account-id>` |
| `aws_s3_bucket_versioning` | **enabled** |
| `aws_s3_bucket_server_side_encryption_configuration` | **SSE-KMS** (`aws:kms`; defaults to the AWS-managed `aws/s3` key when `kms_key_arn` is null) |
| `aws_s3_bucket_public_access_block` | all four flags **true** (public access blocked) |
| `aws_s3_bucket_lifecycle_configuration` | noncurrent versions **expire after 90 days** |
| `aws_s3_bucket_policy` | denies non-TLS (`aws:SecureTransport=false`) access |
| `aws_dynamodb_table.locks` | `cypherx-terraform-locks`, `PAY_PER_REQUEST`, hash key `LockID` (String) |

## Bootstrap (chicken-and-egg)

This module *creates* the backend, so it cannot store its own state there on the
first run. Bootstrap procedure (run once per AWS account):

```bash
# 1. Apply with a local backend (no backend block present in this module).
cd modules/tfstate-backend   # or a thin bootstrap stack that sources this module
terraform init
terraform apply \
  -var bucket_name="cypherx-terraform-state-<account-id>" \
  -var dynamodb_table_name="cypherx-terraform-locks"

# 2. Add an `backend "s3"` config pointing at the bucket just created and migrate:
terraform init -migrate-state
```

After migration, all other stacks (driven by Terragrunt) use this bucket +
table automatically via the root `terragrunt.hcl` `remote_state` block.

## Inputs

| Name | Description | Default |
|------|-------------|---------|
| `bucket_name` | State bucket name | — (required) |
| `dynamodb_table_name` | Lock table name | `cypherx-terraform-locks` |
| `kms_key_arn` | Optional CMK ARN; null ⇒ AWS-managed `aws/s3` key | `null` |
| `noncurrent_version_expiration_days` | Noncurrent version TTL | `90` |
| `tags` | Resource tags | `{}` |

## Outputs

`state_bucket_id`, `state_bucket_arn`, `lock_table_name`, `lock_table_arn`.

## Notes

- `prevent_destroy = true` is set on both the bucket and the lock table — losing
  either is catastrophic.
- No secrets are stored in this module. Encryption keys are referenced by ARN
  only.
