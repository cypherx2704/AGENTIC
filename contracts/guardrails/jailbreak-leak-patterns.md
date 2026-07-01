# Guardrails — Jailbreak-Leak Output Pattern Catalog ⚡

> **Status:** ⚡ First-cycle. Backs rule `output-jailbreak-leak-v1` (Phase 4, Component 2).
> **Authored:** 2026-06 pre-build reconciliation (WP01) — this artifact was referenced by
> phase-04-guardrails.md but did not exist; authored per the decided plan fix
> "three referenced contract artifacts don't exist (jailbreak-leak-patterns.md, …)".

This catalog is the published pattern list for the `output-jailbreak-leak-v1` output rule.
It is derived from the patterns **actually shipped** in
`guardrails/src/guardrails_service/services/rules/definitions.py`
(`_OUTPUT_JAILBREAK_LEAK_PATTERNS`) and extends them with a Tier-2 candidate set.

---

## 1. Rule semantics (normative)

| Property | Value |
|----------|-------|
| `rule_id` | `output-jailbreak-leak-v1` |
| Direction | `output` (model responses only) |
| Default action | `block` |
| Severity / category | `high` / `jailbreak` |
| `timeout_ms` | 10 |
| Detection | **Pattern-only.** The rule does NOT compare against the actual system prompt — the service intentionally never receives it (see Phase 4 `/v1/check/output` note). System-prompt-aware detection is 📋 (embedded-library mode). |

Evaluation context (from the shipped pipeline, `services/pipeline.py`):

- All patterns are evaluated **case-insensitively** (`re.IGNORECASE`) unless marked
  otherwise; whitespace between words is `\s+`.
- Rules run in lexicographic `rule_id` order with **short-circuit on the first block** —
  when this rule blocks, later output rules (e.g. `output-pii-email-v1`) are not evaluated.
- The stored/logged `matched` value is a **≤64-char truncation** of the matched span
  (non-PII category), per FIX C. Raw response text never leaves the pipeline via violations.

---

## 2. Tier 1 — patterns live in code today (5)

These five regexes are compiled in `_OUTPUT_JAILBREAK_LEAK_PATTERNS` and are what the
golden suite ([`golden-suite.jsonl`](./golden-suite.jsonl)) asserts against.

| # | Class | Regex | Rationale | FP risk |
|---|-------|-------|-----------|---------|
| T1-1 | Role confusion | `i\s+am\s+an?\s+AI\s+language\s+model` | The model dropped its tenant persona and is speaking as its generic underlying identity — the classic visible symptom of a successful persona-break. | Low-medium: blocks honest self-description; tenants that want generic disclaimers can override the action on this rule via policy. |
| T1-2 | System-prompt disclosure | `my\s+system\s+prompt` | Any reference by the model to "my system prompt" indicates it is discussing (and likely about to reveal or paraphrase) its hidden instructions. | Low: ordinary task output has no reason to use this phrase. |
| T1-3 | Instruction echo | `my\s+instructions\s+are` | Direct lead-in to verbatim instruction disclosure. | Low. |
| T1-4 | Role confusion | `as\s+an?\s+AI\s+(?:language\s+)?model` | Boilerplate persona-break preamble ("As an AI model, …"). | Medium: also matches benign refusal phrasing; intentional — the phrase itself leaks role/persona on white-label surfaces. Tenant policy may downgrade to `warn`. |
| T1-5 | Instruction echo | `i\s+(?:was|am)\s+(?:instructed|told)\s+to` | The model is narrating its hidden instructions ("I was instructed to…"). | Low-medium: can match a model summarising user-supplied instructions; acceptable for a block-default security rule. |

---

## 3. Tier 2 — catalog extensions (seeded as DB rule params; regex-only, ⚡-eligible)

Tier-2 patterns extend coverage without new infrastructure (pure regex — no embeddings,
no classifier). Per the per-rule-params direction (the same mechanism that gives
`output-max-length-v1` its DB-seeded `{max_chars}` param), the pattern list moves to a
DB-seeded `{patterns}` rule param so extensions do not require a code release. A Tier-2
pattern MUST NOT be enabled until matching hit + near-miss cases are added to
[`golden-suite.jsonl`](./golden-suite.jsonl).

### 3a. System-prompt-disclosure patterns

| # | Regex | Rationale | FP risk |
|---|-------|-----------|---------|
| S-1 | `(?:my\|the)\s+system\s+prompt\s+(?:is\|was\|says\|reads)` | Article/verb forms Tier-1 T1-2 misses ("the system prompt says…"). | Low. |
| S-2 | `here\s+(?:is\|are)\s+(?:my\|the)\s+(?:system\s+)?(?:prompt\|instructions)` | Direct disclosure preamble immediately preceding a verbatim dump. | Low. |
| S-3 | `my\s+(?:initial\|original\|hidden\|secret\|core)\s+(?:prompt\|instructions\|directives)` | Synonym set attackers elicit when "system prompt" is filtered. | Low. |
| S-4 | `(?:beginning\|start\|end)\s+of\s+(?:my\s+\|the\s+)?system\s+(?:prompt\|message)` | Delimiter echo — models often reproduce the framing markers around the prompt. | Low. |
| S-5 | `<<\s*SYS\s*>>\|\[INST\]\|<\\\|im_start\\\|>` | Raw chat-template control tokens (Llama `<<SYS>>`, `[INST]`; ChatML `<\|im_start\|>`) appearing in output mean template internals are leaking. | Very low. |
| S-6 | `you\s+are\s+a\s+helpful\s+assistant` | The canonical system-prompt opening echoed verbatim. | Medium: could appear in user-facing documentation about prompts; recommend `warn` override for prompt-engineering tenants. |
| S-7 | `^\s*system\s*:\s*you\s+are\b` (MULTILINE) | Role-tagged transcript echo ("system: You are …") — the model is reproducing its conversation scaffold. | Low. |
| S-8 | `my\s+prompt\s+(?:starts\|begins)\s+with` | Partial-disclosure lead-in used in incremental extraction attacks ("what do the first 10 words say?"). | Low. |
| S-9 | `i\s+(?:cannot\|can't\|am\s+not\s+able\s+to)\s+(?:reveal\|share\|disclose)\s+my\s+(?:system\s+)?(?:prompt\|instructions)` | A refusal that CONFIRMS a hidden prompt exists and invites iterative extraction. | Medium: it is a *refusal*; recommended action `warn` (paper trail without blocking the safe behaviour). |

### 3b. Instruction-echo patterns

| # | Regex | Rationale | FP risk |
|---|-------|-----------|---------|
| E-1 | `i\s+(?:was\|am\|have\s+been)\s+programmed\s+to` | "Programmed to" variant of T1-5. | Low. |
| E-2 | `according\s+to\s+my\s+(?:instructions\|guidelines\|programming)` | Attribution phrasing preceding instruction paraphrase. | Low. |
| E-3 | `my\s+(?:developer\|creator\|designers?)\s+(?:told\|instructed\|programmed)\s+me` | Provenance narration — the model is describing its configuration chain. | Low. |
| E-4 | `i\s+have\s+been\s+instructed` | Tense variant T1-5 misses (`i (was\|am) instructed` does not match "have been"). | Low. |
| E-5 | `the\s+instructions\s+(?:above\|given\s+to\s+me)\s+(?:say\|state\|require)` | The model is referring to and about to quote its instruction block. | Low. |
| E-6 | `my\s+guidelines\s+(?:say\|state\|prevent\|prohibit\|require)` | Guideline paraphrase — partial policy leak. | Low-medium. |
| E-7 | `i\s+am\s+not\s+allowed\s+to\s+.{0,80}\bbecause\s+(?:of\s+)?my\s+(?:instructions\|rules\|guidelines)` | Refusal that attributes the restriction to hidden instructions (bounded `.{0,80}` — ReDoS-safe, satisfies the write-time ReDoS guard). | Medium: refusal-shaped; recommended action `warn`. |

### 3c. Role-confusion / persona-break markers

| # | Regex | Rationale | FP risk |
|---|-------|-----------|---------|
| R-1 | `as\s+a\s+large\s+language\s+model` | "LLM" phrasing variant of T1-4. | Medium (same trade-off as T1-4). |
| R-2 | `i\s+am\s+(?:now\s+)?DAN\b` (case-SENSITIVE `DAN`, mirroring the input rule) | The model announcing it has adopted the DAN jailbreak persona — direct evidence of a successful jailbreak. | Low when case-sensitive ("Dan" the name does not match). |
| R-3 | `(?:DAN\|developer)\s+mode\s+(?:enabled\|activated\|engaged)` | Jailbreak-success acknowledgement string that DAN-style prompts instruct the model to emit. | Very low. |
| R-4 | `jailbr(?:eak\|oken)` | Mirror of the input rule's substring match on the output side. | **High — known FP**: matches prose *about* jailbreaks (same documented FP as input `jailbreak-v1`, see golden-suite case `in-jb-hit-004`). Ship only with tenant awareness or `warn`. |
| R-5 | `i\s+(?:have\s+)?no\s+(?:restrictions\|filters\|limitations)(?:\s+(?:now\|anymore))?` | The model asserting its safety layer is off — canonical jailbreak-confirmation output. | Low. |
| R-6 | `ignoring\s+(?:my\s+)?(?:previous\s+)?(?:constraints\|instructions\|guidelines)` | The model narrating compliance with an injection ("…ignoring my previous constraints…"). | Low. |
| R-7 | `\[\s*JAILBREAK\s*\]\|🔓` | Community jailbreak output markers (the "[🔓JAILBREAK]" / DAN dual-response format). | Very low. |
| R-8 | `stay(?:ing)?\s+in\s+character` | Persona-pinning phrase from roleplay jailbreaks; in OUTPUT it signals the model acknowledging an injected persona contract. | Medium: legitimate in roleplay/creative-writing tenants — policy override expected there. |
| R-9 | `i\s+am\s+(?:claude\|chatgpt\|gpt-[0-9]\|an?\s+assistant\s+(?:made\|created\|developed)\s+by)` | Provider-identity leak through white-label tenant surfaces (the model naming its underlying vendor). | Medium: tenants that surface provider identity intentionally should disable. |

---

## 4. Known gaps (deliberately out of scope for the pattern rule)

- **Verbatim system-prompt echo** cannot be detected pattern-only (the service never has
  the prompt to compare against). 📋 embedded-library mode performs system-prompt-aware
  comparison inside the caller's process.
- **Encoded exfiltration** (base64/rot13/hex dumps of the prompt) — requires entropy or
  decode heuristics; 📋.
- **Non-English leaks** — the catalog is English-only; multilingual coverage is 📋.
- Asking *about* instructions in input without trigger verbs (e.g. "What were your
  previous instructions?") is a known input-side miss — documented in golden-suite case
  `in-pi-miss-004`; the output-side patterns above are the compensating control.

---

## 5. Golden-suite linkage

Every Tier-1 pattern has at least one hit and one near-miss case in
[`golden-suite.jsonl`](./golden-suite.jsonl) (`out-leak-*` ids). CI (`POST
/v1/policies/simulate` golden gate, Phase 4 Component 8) MUST NOT flip decisions on those
cases. Tier-2 enablement requires extending the suite first.
