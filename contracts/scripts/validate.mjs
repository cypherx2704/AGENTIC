#!/usr/bin/env node
/**
 * Contract schema validator (Phase 0 — Contract exit criteria: "JSON Schemas are valid").
 *
 * Responsibilities:
 *   1. Compile every *.schema.json and *.schema.yaml under the repo with Ajv (draft 2020-12).
 *      A schema that fails to compile is an invalid JSON Schema -> hard failure.
 *   2. Run example fixtures. Any file matching **\/examples\/*.json with shape
 *        { "$schemaRef": "<repo-relative path to a schema>", "valid": [ ... ], "invalid": [ ... ] }
 *      is executed: every `valid` doc MUST pass, every `invalid` doc MUST fail.
 *
 * Pure-ESM, depends only on ajv + ajv-formats + yaml (declared in package.json).
 * Run: `npm run validate`
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { parse as parseYaml } from "yaml";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
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

function loadDoc(file) {
  const raw = readFileSync(file, "utf8");
  return file.endsWith(".yaml") || file.endsWith(".yml") ? parseYaml(raw) : JSON.parse(raw);
}

const files = walk(ROOT);
const schemaFiles = files.filter((f) => /\.schema\.(json|ya?ml)$/.test(f));
const exampleFiles = files.filter((f) => /[\\/]examples[\\/].+\.json$/.test(f));

// One Ajv instance, all schemas registered by $id so cross-$ref resolves.
const ajv = new Ajv2020({ allErrors: true, strict: false, validateSchema: true });
addFormats(ajv);

let errors = 0;
const byId = new Map(); // $id -> validate fn

for (const f of schemaFiles) {
  const rel = relative(ROOT, f);
  let doc;
  try {
    doc = loadDoc(f);
  } catch (e) {
    console.error(`✗ PARSE  ${rel}\n    ${e.message}`);
    errors++;
    continue;
  }
  if (!doc || typeof doc !== "object") {
    console.error(`✗ SHAPE  ${rel} — not an object`);
    errors++;
    continue;
  }
  if (!doc.$schema) console.warn(`⚠ ${rel} — missing $schema keyword`);
  if (!doc.$id) console.warn(`⚠ ${rel} — missing $id keyword`);
  try {
    const validate = ajv.compile(doc);
    if (doc.$id) byId.set(doc.$id, validate);
    byId.set(rel.replace(/\\/g, "/"), validate);
    console.log(`✓ COMPILE ${rel}`);
  } catch (e) {
    console.error(`✗ COMPILE ${rel}\n    ${e.message}`);
    errors++;
  }
}

let exChecked = 0;
for (const f of exampleFiles) {
  const rel = relative(ROOT, f);
  let bundle;
  try {
    bundle = JSON.parse(readFileSync(f, "utf8"));
  } catch (e) {
    console.error(`✗ PARSE  ${rel}\n    ${e.message}`);
    errors++;
    continue;
  }
  const ref = bundle.$schemaRef;
  if (!ref) {
    console.warn(`⚠ ${rel} — example bundle missing $schemaRef, skipped`);
    continue;
  }
  const schemaRel = relative(ROOT, resolve(dirname(f), ref)).replace(/\\/g, "/");
  const validate = byId.get(ref) || byId.get(schemaRel);
  if (!validate) {
    console.error(`✗ REF    ${rel} — cannot resolve $schemaRef "${ref}"`);
    errors++;
    continue;
  }
  for (const [bucket, mustPass] of [["valid", true], ["invalid", false]]) {
    for (const [i, doc] of (bundle[bucket] || []).entries()) {
      exChecked++;
      const ok = validate(doc);
      if (ok !== mustPass) {
        errors++;
        console.error(
          `✗ EXAMPLE ${rel} [${bucket}#${i}] expected ${mustPass ? "PASS" : "FAIL"} got ${ok ? "PASS" : "FAIL"}` +
            (validate.errors ? `\n    ${ajv.errorsText(validate.errors)}` : "")
        );
      }
    }
  }
}

console.log(
  `\n${errors === 0 ? "OK" : "FAILED"} — ${schemaFiles.length} schemas compiled, ${exChecked} example assertions, ${errors} error(s)`
);
process.exit(errors === 0 ? 0 : 1);
