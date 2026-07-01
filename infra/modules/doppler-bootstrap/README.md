# `modules/doppler-bootstrap` — Secrets Bootstrap in Doppler (Components 11 + 20)

Uses the `DopplerHQ/doppler` Terraform provider to create the `cypherx-platform` project, the per-env config
(`dev`/`staging`/`prod`), the per-`(env, namespace)` operator **service tokens** (Component 11), and the
**mandatory secret paths** (Component 20) as placeholders.

Provisioned via `environments/<env>/doppler-bootstrap/terragrunt.hcl`.

## Mandatory secret paths created (Component 20)

The secret **name** encodes the slash-style path that Helm charts resolve by:

| Path | Created for | Purpose |
|------|-------------|---------|
| `service-auth/<svc>/bootstrap_secret` | every service (`var.services`) | Contract 12 — service-to-service auth bootstrap secret |
| `db/<svc>/runtime_password` | every DB-owning service (`var.db_services`) | Contract 14 / Component 14 — runtime DB user |
| `db/<svc>/ddl_password` | every DB-owning service | Contract 14 / Component 14 — Atlas DDL user |
| `ci/github_app_private_key` | once | Component 18 — `cypherx-gitops-bot` GitHub App key |
| `ci/doppler_api_token` | once | Component 11 — long-lived Terraform/operator token |

`var.services` defaults to the first-cycle set (auth, llms, guardrails, memory, rag, xagent, orchestrator,
platform-mgmt, px0-bridge, and the four tools). `var.db_services` is the SharedCore subset that owns a Postgres
schema. Later phases extend these lists (skills — Phase 8, a2a — Phase 10) — do not back-fill here.

> **Placeholders, not real secrets.** Each path is created with a non-secret placeholder value and
> `lifecycle { ignore_changes = [value] }`. Terraform owns the **existence** of the path; the operator writes the
> **real** value after bootstrap (and on rotation), and Terraform never overwrites it. This is why re-applies are
> safe and idempotent even after secrets are populated.

## Per-(env, namespace) operator service tokens (Component 11)

One read-only Doppler service token per namespace in `var.service_token_namespaces`
(`shared-core`, `xagent`, `tools`, `platform-mgmt`, `px0-bridge`), scoped to this env's config. These are emitted
as the **sensitive** `operator_service_tokens` output and consumed by the k8s-addons `operator-bootstrap` stack to
seed the Doppler K8s operator's bootstrap Secret — **not** via manual `kubectl` (Component 11).

## ⚠️ One-time human bootstrap procedure (Component 11 / 20)

The "the Doppler token lives in Doppler" loop needs a starting point. Per environment, **once**:

1. A platform operator exports a **personal** Doppler CLI token in their shell:
   ```bash
   export DOPPLER_TOKEN="<personal-cli-token>"      # personal, MFA-backed
   ```
2. Run the very first apply of this stack:
   ```bash
   terragrunt apply --terragrunt-working-dir environments/<env>/doppler-bootstrap
   ```
   This (a) creates the project/config, the per-namespace operator service tokens, and all placeholder paths, and
   (b) the operator then writes the **long-lived Terraform service token** into Doppler at `ci/doppler_api_token`
   (the placeholder path this stack just created).
3. **Revoke the personal token immediately** after this first apply. From the second apply onward, CI reads
   `ci/doppler_api_token` from Doppler — the personal token is no longer required.
4. Capture the operator name + timestamp in the env's infra changelog. This is the **only** step where a
   human-held secret touches an environment.

> Runbook for the 90-day token rotation: `docs/runbooks/doppler-token-rotation.md` (platform team, 📋).

## Secrets / auth

The provider's `DOPPLER_TOKEN` comes from the environment — the personal token on first apply, `ci/doppler_api_token`
thereafter. **Nothing** is hardcoded in HCL. No real secret value is ever committed; only placeholders are written,
and `ignore_changes` protects live values.

## Inputs (key)

| Variable | Description |
|----------|-------------|
| `env` / `config_name` | Environment (`dev`/`staging`/`prod`) and matching Doppler config name. |
| `service_token_namespaces` | Namespaces that get an operator service token. |
| `services` / `db_services` | Service lists driving the bootstrap + db secret paths. |
| `placeholder_value` | Placeholder written on create; protected by `ignore_changes`. |

## Outputs

`project_name`, `config_name`, `secret_paths`, `operator_service_tokens` (**sensitive**).
