#!/usr/bin/env node
/**
 * Dependency-free structural check (runs with zero `npm install` — Node built-ins only).
 *
 * Purpose: a CI/offline smoke check that the contracts tree is complete and well-formed
 * even when the network is unavailable for ajv/redocly. It does NOT do full JSON-Schema
 * validation (that is `validate.mjs`); it asserts:
 *   1. Every file in the EXPECTED manifest exists.
 *   2. Every *.json parses; every *.schema.json declares $schema + $id.
 *   3. *.schema.yaml files contain $schema and $id markers.
 *
 * Run: `npm run check`
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Every artifact Phase 0 must ship. Keep in sync with phase-00-contracts.md repo structure. */
const EXPECTED = [
  "README.md",
  "CHANGELOG_POLICY.md",
  "jwt/claims.schema.json",
  "jwt/oidc-discovery.md",
  "api/error-format.schema.json",
  "api/pagination.schema.json",
  "api/rerank.schema.json",
  "api/classify.schema.json",
  "api/openapi-base.yaml",
  "a2a/task-request.schema.json",
  "a2a/task-response.schema.json",
  "a2a/delegation.schema.json",
  "a2a/task-types.md",
  "mcp/manifest.schema.json",
  "kafka/event-envelope.schema.json",
  "kafka/topics.md",
  "kafka/events/auth.agent.registered.schema.json",
  "kafka/events/llms.request.completed.schema.json",
  "kafka/events/guardrails.violation.detected.schema.json",
  "kafka/events/agent.task.completed.schema.json",
  "kafka/events/agent.task.failed.schema.json",
  "kafka/events/tenant.created.schema.json",
  "kafka/events/tenant.suspended.schema.json",
  "kafka/events/tenant.plan_changed.schema.json",
  "kafka/events/tenant.deleted.schema.json",
  "kafka/events/memory.usage.recorded.schema.json",
  "skills/skill-definition.schema.yaml",
  "logging/log-format.schema.json",
  "health/endpoints.md",
  "tracing/headers.md",
  "versioning/api-versioning.md",
  "service-auth/service-token.schema.json",
  "tenant/tenant-model.md",
  "tenant/well-known.md",
  "migrations/atlas-conventions.md",
  "smoke-tests/first-cycle.md",
  "smoke-tests/postman-collection.json",
  "approval/approval-token.schema.json",
  "behavior/behavior-policy.schema.yaml",
  "api-keys/api-key-format.md",
  "api-keys/api-key-acl.schema.json",
  "usage/usage-event.schema.json",
  "usage/tenant-quotas.schema.json",
  "onboarding/external-onboarding.md",
  "webhooks/webhook-delivery.md",
];

const IGNORE = new Set(["node_modules", ".git", "dist"]);
function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    if (IGNORE.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
}

let errors = 0;

// 1. Manifest completeness
for (const rel of EXPECTED) {
  if (!existsSync(join(ROOT, rel))) {
    console.error(`✗ MISSING  ${rel}`);
    errors++;
  }
}

// 2/3. Parse + keyword presence
for (const f of walk(ROOT)) {
  const rel = relative(ROOT, f).replace(/\\/g, "/");
  if (f.endsWith(".json")) {
    let doc;
    try {
      doc = JSON.parse(readFileSync(f, "utf8"));
    } catch (e) {
      console.error(`✗ PARSE    ${rel} — ${e.message}`);
      errors++;
      continue;
    }
    if (/\.schema\.json$/.test(f)) {
      if (!doc.$schema) { console.error(`✗ NO$SCHEMA ${rel}`); errors++; }
      if (!doc.$id) { console.error(`✗ NO$ID     ${rel}`); errors++; }
    }
  } else if (/\.schema\.ya?ml$/.test(f)) {
    const raw = readFileSync(f, "utf8");
    if (!/\$schema\s*:/.test(raw)) { console.error(`✗ NO$SCHEMA ${rel}`); errors++; }
    if (!/\$id\s*:/.test(raw)) { console.error(`✗ NO$ID     ${rel}`); errors++; }
  }
}

console.log(`\n${errors === 0 ? "OK" : "FAILED"} — ${EXPECTED.length} expected artifacts, ${errors} problem(s)`);
process.exit(errors === 0 ? 0 : 1);
