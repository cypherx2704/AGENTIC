# Guardrails Service — Feature Dossier

> **Service:** `Shared Core/guardrails` (Phase 04 / WP02+03+07) · Python 3.12 / FastAPI.
> **Purpose:** an accurate map of what's *already implemented*, followed by a **strictly-selected** set of new features worth building next. Every recommendation passed a five-point gate (not already built/gated · ≥2 independent credible sources · respects the cost/latency constraint · measurable by `eval/` · concrete integration point). Rejected candidates are listed with reasons.
> **Method:** multi-agent web research (2024–2026 papers, security advisories, vendor docs) cross-checked against the actual source, then adversarially verified. **Date:** 2026-07-10.
>
> **The decisive constraint.** `/check/input` p95 **30ms** / p99 **50ms** on the cache-hit path; `/check/output` (buffer) **60/100ms**; **xAgent pays 2× per task** (pre + post). **Any synchronous per-request LLM or network call added to the hot path is auto-rejected.** This single rule eliminated the naïve "add an LLM judge" ideas — every survivor below is deterministic CPU-only on the hot path, or lives entirely off it (CI/eval). Tiers: **Tier 1 = no-regret** (no added cost even fully enabled, or measurement/CI-only); **Tier 2 = opt-in** (cost only behind a flag, default path byte-identical).

---

## Part A — Features already implemented

| Capability | State | Where |
|---|---|---|
| **18 built-in rules** — prompt-injection, jailbreak, toxicity (classifier), PII email/phone/credit-card(Luhn)/**SSN(regex+structural)**/IP/address, output-jailbreak-leak, output-toxicity, output-max-length | ✅ (status doc's "11" is stale — SSN/IP/address shipped via migration 0006) | `services/rules/definitions.py` |
| Deterministic HMAC `[REDACTED:cat:hmac]` redaction + per-tenant key rotation/grace | ✅ | `core/redaction.py` |
| Policy CRUD / assign / **simulate** + append-only versioning | ✅ | `services/policy_engine.py`, `api/policies.py` |
| Custom tenant rules (`regex` + `classifier-threshold`) with a **subprocess ReDoS guard** | ✅ | `services/rules/custom.py` |
| Violations endpoint (RLS, keyset-paginated, redaction-safe) | ✅ | `api/violations.py` |
| **Classifier cascade** to llms-gateway `/v1/classify` — escalates only the uncertain `[0.30,0.85)` band, 45ms timeout, once/check, fail-soft to stub | ✅ coded, **default off** (`CLASSIFIER_MODE=stub`) | `services/classifier_client.py` |
| detoxify in-process classifier | ✅ coded (`CLASSIFIER_MODE=detoxify`) | `services/classifier.py` |
| **Presidio PII** (adds PERSON→name, US_PASSPORT→passport) | ✅ coded, **default off** (`GUARDRAILS_PII_PRESIDIO`) | `services/pii_presidio.py` |
| **Prompt-injection untrusted-span spotlighting** → escalate to block | ✅ coded, **default on but inert** unless caller sends `untrusted_spans[]` | `services/injection_defense.py` |
| **Output groundedness** signal (warn-only, never blocks) | ✅ coded, **default off** (`GROUNDEDNESS_ENABLED`) | `services/groundedness.py` |
| Fail-open Valkey policy cache; post-response persist queue (off the decision path) | ✅ | `services/policy_cache.py`, `db/persist_queue.py` |
| Eval harness: per-label **precision/recall/F1 + p50/p99 vs SLO**, CI floors 0.90 | ✅ | `eval/run_eval.py` |

**Known gaps / facts:** passport/name exist **only** behind off-by-default Presidio (no default-path built-in); no `semantic` custom-rule type; no async check mode; no topic blocklist; no policy inheritance; no event-driven hot reload; no `audit` verdict; no trend dashboards; no Llama/Prompt Guard split pod; no streaming window/post modes; no hallucination LLM judge; no output-format validation. **The 55-row `contracts/guardrails/golden-suite.jsonl` exists but is UNWIRED** from the service eval/CI (live eval uses a separate 21-row `eval/golden_set.jsonl`); the spec's custom-rule decision-flip CI gate is not implemented.

> **Implication for "what to add":** a large "additive safety" layer (cascade, Presidio, spotlighting, groundedness) is already coded and gated — those are tuning/enabling, not new work. And the SLO means the highest-value new *detection* must be deterministic and CPU-only. That's exactly what the survivors are.

---

## Part B — Recommended features

### Tier 1 (no-regret) — Detection quality (deterministic, hot-path-safe)

#### B1. Pre-detection input canonicalization (Unicode de-obfuscation) · effort **M**
**What.** Canonicalize input into a **detection view** before the injection/jailbreak/PII regexes run, in layers:
- **Layer A (Tier 1, always-on):** strip Unicode default-ignorable / format(Cf) / control(Cc) code points used to hide or fragment payloads — zero-width (U+200B–200D, U+FEFF), the **Tags block** (U+E0000–E007F, "ASCII smuggling"), and **bidi** overrides/isolates (U+202A–202E, U+2066–2069, "Trojan Source"). Deterministic, no false-positive risk on legitimate text.
- **Layer B (Tier 2, opt-in):** **NFKC** compatibility fold (fullwidth/ligature/superscript → canonical ASCII/BMP).
- **Layer C (Tier 2, opt-in):** **UTS #39 confusables skeleton** fold (cross-script homoglyphs — Cyrillic/Greek/Cherokee look-alikes → Latin skeleton), which NFKC provably cannot do.

Raw text is left untouched for redaction/HMAC; only the block-action injection/jailbreak detectors read the detection view first (avoiding any offset-map issue for PII redaction).

**Why high-impact.** All 18 detectors match raw bytes/regex. An attacker who splices a zero-width space into `ig​nore previous instructions`, Tag-encodes the payload, uses bidi controls, writes a card/SSN in fullwidth digits, or swaps in Cyrillic homoglyphs **defeats every regex/Luhn/structural check** while the downstream LLM still reads the intended text. This is verified against the code (Python regex whitespace does not match U+200B; `ord()-48` Luhn breaks on fullwidth digits). Empirical guardrail studies measure **~70–72% bypass rates** from exactly these tricks against commercial injection/jailbreak classifiers. Fold-then-detect collapses an unbounded obfuscation space back onto the finite patterns the rules already cover.

**Cost & latency.** Layer A: one linear `str.translate` scan over rate-limit-bounded input — single-digit **microseconds**, a no-op on clean ASCII (all current traffic). Layer B/C: one `unicodedata.normalize('NFKC', …)` + an O(n) confusables dict lookup — still pure CPU, no network/model, so **cannot breach the SLO**. Layers B/C are opt-in (default off) because they can alter legitimate non-ASCII/non-Latin text (precision risk, not latency risk); the CI precision floor 0.90 guards regressions.

**Eval metric moved.** Per-label recall/F1 for `jailbreak`, `injection`, and PII labels, via a named golden-set slice of invisible-char / Tag-block / bidi / fullwidth / homoglyph variants of existing rows (currently false negatives; recovered by canonicalization). Benign-slice precision guards against fold-induced false positives.

**Integration point.** New `core/normalization.py` (`canonicalize()`), called once per check in `services/pipeline.py:evaluate()` (or `api/check.py`) to build a `detection_text` passed via a new additive `RuleContext.detection_text` field (mirroring `precomputed_toxicity`/`untrusted_spans`), consumed by `detect_prompt_injection`/`detect_jailbreak`. Layer A always-on; gate B/C behind `INJECTION_NORMALIZE` / `GUARDRAILS_CONFUSABLES_FOLD` flags in `core/config.py`; build the confusables map once in `main.py` lifespan. Extend `eval/golden_set.jsonl`. *(Note: extending canonicalization to PII **redact** rules would require an offset map back to the original text to preserve the "raw PII never leaves" invariant — scope to block-only rules first.)*

**Evidence.**
- Defending LLM applications against Unicode character smuggling — AWS Security Blog: https://aws.amazon.com/blogs/security/defending-llm-applications-against-unicode-character-smuggling/
- Bypassing LLM Guardrails: An Empirical Analysis of Evasion Attacks — arXiv 2504.11168 (measured ASR: Azure Prompt Shield 71.98%, Meta Prompt Guard 70.44%, NeMo 72.54% via zero-width/tags/homoglyph): https://arxiv.org/abs/2504.11168
- Trojan Source: bidirectional Unicode control attack, CVE-2021-42574 (Boucher & Anderson) — https://access.redhat.com/security/vulnerabilities/RHSB-2021-007
- UTS #39: Unicode Security Mechanisms — `skeleton()` + confusables data: https://www.unicode.org/reports/tr39/
- SilverSpeak: Evading AI-Generated Text Detectors using Homoglyphs — arXiv 2406.11239 (homoglyphs drop detector MCC to worse-than-random): https://arxiv.org/abs/2406.11239

**Honest caveat.** Layer A is a clean Tier-1 win. Layers B/C fold legitimate non-Latin text and are correctly opt-in. This is the single highest-value detection addition — it hardens the *entire* deterministic layer at once.

#### B2. Corpus-derived jailbreak/injection signature pack (Aho-Corasick) · effort **M**
**What.** Distill a versioned signature set from published attack corpora — CCS'24 In-The-Wild Jailbreak Prompts (1,405 labeled real jailbreaks) and JailbreakBench/JBB-Behaviors — into a normalized phrase/template index compiled **once** at import into an Aho-Corasick automaton, augmenting today's ~6 hand-written regexes per list with hundreds of high-signal templates (DAN/AIM/STAN variants, role-play openers, privilege-escalation phrasings).

**Why high-impact.** `_JAILBREAK_PATTERNS` and `_PROMPT_INJECTION_PATTERNS` are ~6 canonical phrases each; the published corpora show the real attack surface is far larger, so recall is capped by hand curation. A corpus-backed pack lifts recall on an evidence base; Aho-Corasick keeps the scan **O(text length) regardless of pattern count**, so there's no latency regression even at hundreds of signatures — and it's strictly better-scaling than today's per-pattern `finditer` loop.

**Cost & latency.** Tier 1: build the automaton once at import (checked-in data file). Query time is a single O(n + matches) pass, no LLM/network. Optional pure-Python automaton avoids even a C-extension dependency.

**Eval metric moved.** Per-label recall/F1 for `jailbreak`/`injection`; precision on those + benign is the guard (CI floor 0.90). Strengthened by wiring the unused 55-row `contracts/guardrails/golden-suite.jsonl` (needs a small schema adapter).

**Integration point.** `services/rules/definitions.py`: augment `_JAILBREAK_PATTERNS`/`_PROMPT_INJECTION_PATTERNS` and their detect functions with a compiled signature index loaded from a checked-in data file, keeping the `prompt-injection-v1`/`jailbreak-v1` RuleSpec IDs and RuleHit/decision shape unchanged; `eval/run_eval.py` for metric/golden-suite wiring.

**Evidence.**
- "Do Anything Now": Characterizing In-The-Wild Jailbreak Prompts (Shen et al., ACM CCS 2024) — https://arxiv.org/abs/2308.03825
- JailbreakBench: An Open Robustness Benchmark (NeurIPS 2024 D&B) — https://arxiv.org/abs/2404.01318

**Honest caveat.** A static signature pack has a shelf life — the same papers show in-the-wild attacks are obfuscated and evolving, so it won't generalize to novel attacks (pairs naturally with B1's canonicalization). It **augments** the two existing rules rather than adding a category; the win is measurable recall on corpus-derived labels, with precision guarded by the CI floor. Best sourced from a periodically-refreshed corpus.

#### B3. ICAO 9303 MRZ passport detection (regex + check digits) · effort **M**
**What.** Detect machine-readable-zone (MRZ) blocks (TD1/TD2/TD3) with a structural regex, then confirm with the ICAO Doc 9303 7-3-1 mod-10 check digits over the document number, DOB, expiry, and composite field — the same "regex + deterministic validation" shape already used for credit-card (Luhn) and SSN, applied to passports.

**Why high-impact.** Passport detection currently exists **only** via the gated Presidio US_PASSPORT recognizer — a weak bare-number regex with no checksum, requiring spaCy. A self-contained MRZ detector runs on the **default** hot path, adds a full identity document (with DOB/expiry), and because **four independent check digits must all pass**, false positives are astronomically unlikely — so it lifts passport recall for OCR'd/KYC/travel-document text at essentially zero FP cost.

**Cost & latency.** Tier 1: an anchored, fixed-width `[A-Z0-9<]{30|36|44}` regex (ReDoS-safe) + integer arithmetic over ≤3 lines — one extra linear scan comparable to the existing CC/SSN/address rules, sub-millisecond, no model/network. Purely additive (fires only on validated MRZ). Ships default-on like Luhn/SSN.

**Eval metric moved.** Passport per-label precision/recall/F1 — add `label:"passport"` rows (MRZ positives → redact/block; checksum-failing near-misses → allow); the CI floor guards it.

**Integration point.** `services/rules/definitions.py`: add `_mrz_check_digit()` mirroring `_luhn_ok`, the TD1/TD2/TD3 regexes, and `detect_pii_passport_mrz` mirroring `detect_pii_ssn`; register `pii-passport-mrz-v1` in INPUT_RULES + an `output-pii-passport-mrz-v1` twin. Category `passport` already exists in `core/redaction.py PII_CATEGORIES`, so redaction/HMAC needs no change. *(Add a `guardrails.rules` seed row per new rule_id, else `registry._overlay` flips `/readyz` to 503.)*

**Evidence.**
- ICAO 9303 MRZ check digits — idcheck.dev: https://idcheck.dev/icao-9303-check-digits/
- MRZ check digits explained — TrustDocHub: https://trustdochub.com/en/mrz-check-digits/
- ICAO MRZ check-digit calculator (algorithm corroboration) — planetcalc: https://planetcalc.com/9535/

**Honest caveat.** Sources are secondary explainers of a published international standard (ISO/IEC 7501 / ICAO Doc 9303) rather than the primary spec, but the near-perfect precision follows directly from the checksum math — exactly like the already-shipped Luhn rule.

### Tier 1 (no-regret) — Evaluation & CI hardening (zero runtime cost)

> **Foundational enabler (do this first):** wire the unused **55-row `contracts/guardrails/golden-suite.jsonl`** into the live eval. It exists, carries `expect_decision`/`expect_rules`, and is more representative than the 21-row `eval/golden_set.jsonl` the harness uses today. B4–B6 all build on it. Near-zero cost, high leverage — and a strictly higher-value fix than statistical tricks on 21 rows (see Part C).

#### B4. Decision-flip / negative-flip regression gate · effort **S**
**What.** Freeze the allow/warn/redact/block decision for every golden row into a checked-in baseline (`eval/baseline_decisions.json`); on every CI run, diff decisions against it. **Gate on "negative flips":** a safety-critical `block→allow` or `redact→allow` change fails the build unconditionally; benign `allow→block` false-positive flips get an explicit budget.

**Why high-impact.** Aggregate P/R/F1 can stay ≥0.90 while individual high-stakes decisions silently regress when a tenant edits a custom regex/threshold, someone tunes an escalation band, or `CLASSIFIER_MODE` is flipped. The negative-flip literature shows overall error can improve while per-example predictions regress — and one loosened pattern re-opening a jailbreak class is exactly what the mean-F1 floor can't see. This *is* the spec's custom-rule decision-flip gate that the baseline lists as not implemented.

**Cost & latency.** Tier 1, zero hot-path impact. CI/eval-only, reusing the deterministic in-process `pipeline.evaluate()` over ~55 rows. Only a CI job is added.

**Eval metric moved.** Adds a Negative-Flip-Rate gate to `run_eval.py:summarize()`: safety-critical downgrades must equal 0 (hard fail); benign FP flips within budget. Complements exact-decision accuracy + per-label P/R/F1.

**Integration point.** New `eval/regression_gate.py` + checked-in `eval/baseline_decisions.json`; reuse `services/pipeline.py:evaluate()` via the `run_eval.py:_run_one` path; wire `contracts/guardrails/golden-suite.jsonl` as the frozen corpus; enforce in `tests/test_eval_harness.py` next to the MIN_* floors.

**Evidence.**
- Positive-Congruent Training: Towards Regression-Free Model Updates (Yan et al., CVPR 2021; defines Negative Flip Rate) — https://arxiv.org/abs/2011.09161
- MUSCLE: A Model Update Strategy for Compatible LLM Evolution (Apple, 2024) — https://arxiv.org/pdf/2407.09435
- FlipGuard: Defending Preference Alignment against Update Regression (2024) — https://arxiv.org/pdf/2410.00508

**Honest caveat.** The offline eval uses the stub classifier + builtin default policy, so the custom-rule/classifier-bump scenarios aren't fully exercised without further harness extension — but the frozen per-row baseline + asymmetric negative-flip gate is a real, non-duplicate regression guard over the deterministic rule/redaction core. **Lowest effort, high leverage — the recommended starting point.**

#### B5. Standardized adversarial red-team eval split (per-category ASR) · effort **M**
**What.** Grow the suite with a static, versioned red-team split (`eval/redteam_set.jsonl`) snapshotted **offline** from published human-vetted benchmarks (JailbreakBench JBB-Behaviors, HarmBench, AdvBench) and/or a one-time dump of NVIDIA garak's probe catalog. Add **Attack Success Rate** (fraction of adversarial prompts the guardrail fails to block) as a per-category metric with a CI ceiling.

**Why high-impact.** The live eval is 21 generic, hand-written rows whose attacks closely mirror the templates the rules were written against — it measures the guardrail against itself. Standardized benchmarks supply diverse, categorized, real-world evasion strategies the rules were **not** tuned on, exposing coverage holes. ASR is the field-standard safety metric.

**Cost & latency.** Tier 1: static corpus snapshotted offline; CI runs the existing in-process `evaluate()` over more rows — no LLM/network/async, hot path untouched.

**Eval metric moved.** Per-category ASR (= 1 − recall) over `redteam_set.jsonl`, gated as a **ratchet** (freeze the measured baseline, fail on regression) — not an absolute floor, since the stub lexicon will start with high ASR on a genuinely diverse split.

**Integration point.** New `eval/redteam_set.jsonl` (same schema as `golden_set.jsonl`); extend `run_eval.py:load_golden()`/`summarize()`; ratchet assertion in `tests/test_eval_harness.py`; exercises the real `definitions.py` patterns via `pipeline.evaluate()`.

**Evidence.**
- JailbreakBench (NeurIPS 2024 D&B; JBB-Behaviors + standardized ASR) — https://arxiv.org/abs/2404.01318
- HarmBench (Mazeika et al., ICML 2024; categorized corpus + ASR) — https://arxiv.org/abs/2402.04249
- NVIDIA garak — LLM vulnerability scanner (50+ probe modules) — https://github.com/NVIDIA/garak

**Honest caveat.** `ASR = 1 − recall` is a reframe of an already-reported metric, so the real deliverable is the **independent corpus + per-category ratchet**, and the gate must be a ratchet not a floor. Still worthwhile for a safety service whose entire eval currently tests attacks it wrote to match its own rules.

#### B6. CheckList-style metamorphic behavioral tests (MFT / INV / DIR) · effort **M**
**What.** Add three behavioral test types generated deterministically from seed rows: **MFT** (canonical known-attack/benign templates pinned to a required decision), **INV** (label-preserving perturbations — leetspeak, homoglyphs, zero-width, case — where the decision must **not** flip), **DIR** (splicing a known attack into a benign prompt must only move toward block). Report failure rate per type.

**Why high-impact.** Regex + classifier-threshold guardrails are notoriously evadable by trivial meaning-preserving perturbations — exactly what INV encodes. CheckList (ACL 2020 best paper) found large behavioral bugs in commercial NLP models holding high aggregate accuracy; metamorphic testing solves the oracle problem for unlabeled inputs where you know the decision must stay invariant. Verified against the code: `re.IGNORECASE` regexes + a literal stub lexicon mean leetspeak/homoglyph/zero-width perturbations deterministically flip `block→allow` on jailbreak/injection/toxic seeds.

**Cost & latency.** Tier 1: deterministic string transforms at CI/eval time only; `pipeline.evaluate()` and the hot path are byte-identical. No LLM/network.

**Eval metric moved.** Adds invariance-failure-rate and directional-violation-rate to `run_eval.py` alongside per-label P/R/F1; surfaces per-perturbation recall drops under leet/homoglyph/zero-width.

**Integration point.** New `eval/behavioral.py` (deterministic perturbation + attack-splice generators seeded from `golden_set.jsonl` + the golden suite); extend `run_eval.py` to compute the two new rates; CI floor in `tests/test_eval_harness.py`; add a `test_type` field (mft|inv|dir). No service/pipeline/DB code changed.

**Evidence.**
- Beyond Accuracy: Behavioral Testing of NLP Models with CheckList (Ribeiro et al., ACL 2020 Best Paper; defines MFT/INV/DIR) — https://aclanthology.org/2020.acl-main.442/
- Testing ML classifiers by metamorphic testing (Xie et al., JSS 2011) — https://www.sciencedirect.com/science/article/abs/pii/S0164121210003213

**Honest caveat.** MFT largely re-expresses the existing exact-decision check — but INV+DIR are net-new and directly surface the trivial-perturbation evasions that are a headline guardrail failure mode. Strong synergy with B1: INV tests prove B1's canonicalization actually closes the obfuscation gap.

---

### Tier 2 (opt-in; default path byte-identical)

#### B7. Per-request canary-token leak detector (output rule) · effort **S**
**What.** A new OUTPUT rule that scans model output for caller-supplied high-entropy canary token(s) the caller embedded in its own system prompt; any occurrence (exact + normalized/de-spaced/hex/base64-tolerant match) means the system prompt/context leaked → block. Canaries arrive via a new optional `canary_tokens[]` field, mirroring the existing `untrusted_spans[]`/`grounding[]`.

**Why high-impact.** The existing `output-jailbreak-leak-v1` matches generic phrases ("my system prompt", "as an AI language model") — it false-positives on benign self-reference and misses novel/paraphrased leaks. A per-request random canary is a **near-zero-false-positive, phrasing-independent** signal of successful prompt-injection exfiltration — the highest-precision leak detector available, recommended by AWS, Rebuff/LangChain, and OWASP LLM01. Pure deterministic string search.

**Cost & latency.** Tier 2: field absent (default) ⇒ byte-identical (inert exactly like `untrusted_spans[]`). Enabled ⇒ a bounded O(n) scan for each canary + a few precomputed encoded variants. No LLM/network — doesn't touch the auto-reject.

**Eval metric moved.** Precision (primary) + recall on a new `leak` output label; the new rule should show precision ≈1.0 vs `output-jailbreak-leak-v1`'s noisier behavior.

**Integration point.** `services/rules/definitions.py`: `detect_output_canary_leak()` + `output-canary-leak-v1` RuleSpec + `canary_tokens` on `RuleContext`; `models/check.py`: optional `canary_tokens` on `CheckRequest` (`extra="forbid"` means it must be declared); `api/check.py`: thread `body.canary_tokens` into `RuleContext`, gated by `CANARY_LEAK_ENABLED`; `core/config.py` flag; `eval/run_eval.py:_run_one` reads it (already does this for `untrusted_spans`).

**Evidence.**
- Designing for the inevitable: System prompt leakage and mitigations — AWS Security Blog: https://aws.amazon.com/blogs/security/designing-for-the-inevitable-system-prompt-leakage-and-mitigations-in-generative-ai-applications/
- Rebuff: Detecting Prompt Injection Attacks (canary tokens) — LangChain Blog: https://www.langchain.com/blog/rebuff
- OWASP Top 10 for LLM Applications — canary tokens (LLM01) — https://github.com/OWASP/www-project-top-10-for-large-language-model-applications/issues/288

**Honest caveat.** A canary only catches verbatim/lightly-obfuscated exfiltration — a full semantic paraphrase that omits the token still evades it. The real win is **precision**, not a recall panacea. Lowest-effort Tier-2 item (S).

#### B8. Native context-window PII validation → default-path passport/name · effort **M**
**What.** A deterministic, spaCy-free context enhancer: each regex PII hit is admitted/boosted only when a configurable supporting term (`"passport"`, `"document number"`, `"SSN"`, honorifics like `"Mr./Dr./my name is"`) appears within an N-char proximity window — the exact precision mechanism Presidio's context enhancer and Google DLP hotword rules use, implemented natively. This unlocks a **hot-path-safe** built-in passport/national-ID (and honorific-gated name) detector that bare regex can't do at acceptable precision.

**Why high-impact.** Passport and name today exist only behind off-by-default Presidio spaCy; real ML NER for names (e.g. GLiNER ~75ms CPU) **blows the 30/50ms SLO**, so the only viable default-path route to these types is deterministic context-gated regex. A weak passport/name regex alone is a false-positive machine; gating it on a proximity keyword window makes it deployable as a microsecond substring scan.

**Cost & latency.** Tier 2: per hit, a bounded substring scan over N surrounding chars against a small term set — microseconds, no LLM/network/ingest cost. Behind `GUARDRAILS_PII_CONTEXT_VALIDATION` (default off, fail-soft) so the default path stays byte-identical (opt-in because always-on passport/name regex carries FP risk to the pinned golden verdicts).

**Eval metric moved.** Per-label precision/recall/F1 for new `passport`/`name` labels + overall precision; add context-present hits (redact) and context-absent near-misses (allow) to pin that the gate both fires and suppresses. CI floor 0.90 guards default-path FPs.

**Integration point.** `services/rules/definitions.py`: `_context_supported(text, span, terms, window)` helper + `pii-passport-v1`/optional `pii-name-v1` RuleSpecs (categories already in `PII_CATEGORIES`); `GUARDRAILS_PII_CONTEXT_VALIDATION` + lexicons/window in `core/config.py`; **a `guardrails.rules` seed row per new rule_id** (else `registry._overlay` → 503).

**Evidence.**
- Context enhancement — Microsoft Presidio: https://microsoft.github.io/presidio/tutorial/06_context/
- Customizing match likelihood (hotword rules) — Google Cloud Sensitive Data Protection: https://cloud.google.com/dlp/docs/creating-custom-infotypes-likelihood
- Towards Contextual Sensitive Data Detection (Telkamp & Hulsebos, 2025) — https://arxiv.org/abs/2512.04120

**Honest caveat.** The "cuts FPs on *existing* PII types" framing is oversold — the pinned near-miss rows (Luhn-failing CC, too-short phone) are already correctly allowed by existing structural validation. The real, defensible win is **default-path passport/national-ID and honorific-gated name coverage** that today needs gated spaCy Presidio. Overlaps with B3 (MRZ) for passports — B3 is the higher-precision path for travel documents; B8 covers the broader passport/name-in-prose case. Sequence B3 first.

---

## Part C — Rejected candidates (strictness applied)

The SLO did most of the filtering — every "add an LLM classifier/judge on the hot path" idea was auto-rejected. These four survived to verification and still failed:

| Candidate | Failing gate | Reason |
|---|---|---|
| In-process distilled INT8 transformer (Prompt Guard 2 / DeBERTa-xsmall via ONNX) for the "uncertain injection/jailbreak band" | Cost/latency unsound + evidence | The claim that "a hard per-call timeout bounds p99" is **false** for this code — `pipeline.evaluate()` runs detectors synchronously and measures elapsed time *post-hoc* without interrupting; an ONNX forward pass runs to completion and a wall-clock timeout can't cancel a CPU matmul, degrading tail latency under contention on a 30/50ms path. The CPU-latency number is a GPU figure × an INT8 multiplier. Also: there is **no** uncertain injection/jailbreak band — those detectors are regex-only; the `[0.30,0.85)` band is toxicity-only. |
| Off-path async "audit lane" (run heavy checks on the persist queue) | Evidence mismatch + cost | The cited OpenAI/NeMo async-guardrail patterns still gate the *current* response; this proposal explicitly gates only *future* turns — neither source endorses that design. The persist queue is an in-process asyncio worker sharing the event loop / psycopg pool (max 10), so "zero hot-path cost under load" is unproven; a real LLM judge adds token cost. Also unmeasurable on the stub harness and bundles ~5 separate gap items, some against the "raw PII never leaves" invariant. |
| Content-addressed exact-match verdict cache | Evidence + **correctness defect** | Cited sources measure *semantic* caching (the mechanism this rejects); exact-match hit rate is weakest on the novel/adversarial traffic a safety service cares about. Worse, the proposed key omits load-bearing verdict inputs (`input_text`, `untrusted_spans`, and custom-rule version chains separate from `policy_version`), so it can return a **stale ALLOW on a real output leak** — the exact hazard it's meant to prevent. |
| Statistical CI gating (bootstrap CIs + McNemar on P/R/F1) | Evidence + measurability | Effectively one credible source, and the significance machinery is misused at **n=21** (bootstrap CIs badly undercover; McNemar is powerless with 0–2 discordant pairs on a saturated suite). It changes the *gate*, not any metric the harness measures, so it's self-referential. The real root cause is sample size — **wire the 55-row golden suite (B4) instead**; more representative data beats statistics layered on 21 rows. |

**Cross-cutting lesson.** Guardrails' quality ceiling is set by (a) the tight SLO, which forces new detection to be deterministic/CPU-only, and (b) a **thin, self-referential eval** (21 rows the rules were written against, plus an unwired 55-row suite). That's why the recommendation set is deliberately weighted toward **deterministic de-obfuscation (B1–B3)** and **eval/CI hardening (B4–B6)** — the two things that raise real delivered safety without touching the hot-path budget.
