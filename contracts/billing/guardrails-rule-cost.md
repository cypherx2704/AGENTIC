# Guardrails — Per-Rule Metering Cost Table ⚡

> **Status:** ⚡ First-cycle (values seeded with the WP07 migration that adds the column).
> **Authored:** 2026-06 pre-build reconciliation (WP01) — this artifact was referenced by
> phase-04-guardrails.md (billing component) but did not exist; authored per the decided
> plan fix "Author all three artifacts in contracts/ … rule-cost table matching
> `rules.estimated_cost_usd_per_call`".

## Purpose

Guardrails check metering follows the single-owner rule (Contract 14): usage events carry
**units + correlation ids only**; unit costs live in the owning service's own table. For
Guardrails, the per-rule unit cost is the column
**`guardrails.rules.estimated_cost_usd_per_call`** (numeric, USD per rule evaluation).
This document is the human-readable contract mirror of the seeded values — the DB column
is the runtime authority (nothing-hardcoded rule); code never embeds these numbers.

Billing rollup multiplies rule-evaluation counts from `cypherx.guardrails.check.completed`
usage events by the value current in `guardrails.rules` at rollup time.

## Seeded values (platform rules, tenant_id IS NULL)

| `rule_id` | Mechanism | `estimated_cost_usd_per_call` | Basis |
|-----------|-----------|------------------------------:|-------|
| `prompt-injection-v1` | regex | 0.000001 | CPU-trivial pattern scan |
| `pii-email-v1` | regex | 0.000001 | CPU-trivial pattern scan |
| `pii-phone-v1` | regex + digit filter | 0.000001 | CPU-trivial pattern scan |
| `pii-credit-card-v1` | regex + Luhn | 0.000001 | CPU-trivial pattern scan |
| `jailbreak-v1` | regex | 0.000001 | CPU-trivial pattern scan |
| `toxicity-v1` | classifier (detoxify, CPU) | 0.000050 | torch-cpu RoBERTa inference, `guardrails:ml` image |
| `output-pii-email-v1` | regex (input-echo set diff) | 0.000001 | CPU-trivial pattern scan |
| `output-pii-credit-card-v1` | regex + Luhn | 0.000001 | CPU-trivial pattern scan |
| `output-jailbreak-leak-v1` | regex (pattern catalog) | 0.000001 | CPU-trivial pattern scan |
| `output-toxicity-v1` | classifier (detoxify, CPU) | 0.000050 | torch-cpu RoBERTa inference, `guardrails:ml` image |
| `output-max-length-v1` | length check | 0.000001 | O(1) length comparison |

Notes:

- The classifier rows are priced for the prod profile (`CLASSIFIER_MODE=detoxify`,
  `guardrails:ml` image). The dev/CI stub (`guardrails:slim`) executes the same rule ids;
  the seeded estimate is unchanged (it is an *estimate*, not a measured invoice line).
- Range anchor from the phase plan: 0.000001 (cheap regex) … 0.005 (Llama Guard 2
  classifier inference). **Llama Guard 2 is 📋 enterprise** — when introduced, its rule
  row is seeded at 0.005.

## Custom tenant rules (Component 8)

| Custom rule `type` | `estimated_cost_usd_per_call` | Notes |
|--------------------|------------------------------:|-------|
| `regex` | 0.000001 | First cycle |
| `classifier-threshold` | 0.000050 | First cycle; re-uses the loaded classifier, threshold from rule params |
| `semantic` | 0.000200 + embedding usage | 📋 — gated on WP06 embeddings; the embedding call itself is billed through LLMs usage (not double-counted here) |

(`chain` type was deleted by the 2026-06 reconciliation; it has no cost row.)

## Update policy

- Values change ONLY via an administrative migration on `guardrails.rules` plus a PR to
  this document in the same change set; CI cross-checks the two.
- Adding a rule (platform or Tier-2 pattern promotion) requires a cost row before the
  rule can reach `status='active'`.
