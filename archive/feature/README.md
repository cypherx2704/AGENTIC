# Feature Research — RAG · Memory · Guardrails

> **What this is.** An evidence-backed dossier on where the three Shared Core quality services stand today and — strictly selected — what to build next to raise their quality. One file per service; this index carries the selection criteria, the ranked cross-service shortlist, and the full summary table.
>
> **How it was produced.** Three code-verified baseline audits, then multi-agent web research (13 technique areas across the three services; 2024–2026 papers, framework/vendor docs, security advisories), then an **adversarial verifier per candidate** applying the gate below. **Date:** 2026-07-10.
>
> **Read the service files for detail:** [rag.md](rag.md) · [memory.md](memory.md) · [guardrails.md](guardrails.md).

---

## Selection criteria (the strict gate)

The user asked for high-impact features that **do not slow down or cost more**, and explicitly: *no sloppy filler.* Every recommendation had to pass **all five**:

1. **Not already built or gated.** Each service already ships the "obvious" upgrades flag-gated (RAG: hybrid/rerank/contextual-ingest; Memory: composite scoring/contradiction/consolidation; Guardrails: classifier-cascade/Presidio/injection-spotlight/groundedness). Re-suggesting these = rejected.
2. **Evidence-backed impact** — ≥2 independent credible sources, not one blog.
3. **Respects cost/latency.** **Tier 1 (no-regret):** no added cost/latency even fully enabled — better algorithm, one-time cost, or measurement. **Tier 2 (opt-in):** cost only behind an explicit flag with a guard + fail-soft; default path byte-identical. For Guardrails, any synchronous LLM/network call on the hot path (30/50ms input SLO) = auto-reject.
4. **Measurable** by the service's own `eval/` harness (or a small named extension).
5. **Concrete integration point** — a named file/interface/flag.

**Result:** **18 features recommended, 17 rejected.** The rejected lists (in each service file) are as important as the survivors — they show the bar was real.

---

## Ranked shortlist — start here

If only a handful get built, build these, in order. Ranked by impact × no-regret × low effort.

| # | Service | Feature | Tier | Effort | Why it's top |
|---|---|---|---|---|---|
| 1 | Guardrails | **Unicode input canonicalization** (strip invisible/tag/bidi; opt-in NFKC + homoglyph fold) | 1 | M | Closes a measured **~70% bypass hole** across the *entire* deterministic rule layer; deterministic, hot-path-safe. |
| 2 | Guardrails | **Decision-flip / negative-flip regression gate** (+ wire the unused 55-row golden suite) | 1 | S | Lowest effort; stops silent safety downgrades from rule/classifier edits that the mean-F1 floor can't see. |
| 3 | Memory | **Content-hash embedding cache** (Valkey) | 2 | M | *Reduces* cost+latency (net-negative) on repeated store/query text; Valkey already present. |
| 4 | Memory | **HNSW `ef_search` + explicit build params** | 2 | M | Fixes a real recall-truncation defect: default `ef_search=40` silently caps the ×4/×8 oversample. |
| 5 | Memory | **`halfvec` (16-bit) HNSW index** | 1 | M | 2× smaller index / better RAM residency at ~identical recall — clean no-regret. |
| 6 | RAG | **Context-Precision (ID-based MAP) metric** | 1 | S | Cheap; adds the missing precision axis and is the prerequisite for honestly measuring the rest. |
| 7 | Guardrails | **ICAO 9303 MRZ passport detection** (regex + check digits) | 1 | M | High-value default-path identity-doc PII at ~zero false-positive cost (same shape as shipped Luhn/SSN). |
| 8 | Guardrails | **Corpus-derived jailbreak/injection signature pack** (Aho-Corasick) | 1 | M | Lifts recall on real published attack corpora; O(n) scan regardless of pattern count. |
| 9 | RAG | **LLM query decomposition** (multi-hop) | 2 | M | Biggest retrieval-quality lever for compound questions; reuses the already-coded reranker. |
| 10 | Memory | **Salient-fact extraction at ingest** (Mem0/LangMem) | 2 | M | Un-dilutes multi-fact embeddings — the core specific-fact retrieval lever. |

---

## Full summary — all recommendations

### RAG — [rag.md](rag.md)  ·  1 Tier-1, 2 Tier-2, 9 rejected
| Feature | Tier | Effort | Eval metric moved |
|---|---|---|---|
| Context-Precision (ID-based MAP) in the harness | 1 | S | Context-Precision@k (new) |
| LLM query decomposition (multi-hop) | 2 | M | MRR@10 / recall@10 (multi-hop slice) |
| Multi-query expansion via RRF (RAG-Fusion) | 2 | M | recall@k (recall lever; pair with rerank) |

### Memory — [memory.md](memory.md)  ·  1 Tier-1, 6 Tier-2, 4 rejected
| Feature | Tier | Effort | Eval metric moved |
|---|---|---|---|
| `halfvec` (16-bit) HNSW index | 1 | M | recall@k (lossy — float16 delta) |
| Content-hash embedding cache | 2 | M | `embed_calls_total` ↓ (recall@k unchanged) |
| HNSW `m`/`ef_construction` + `ef_search` | 2 | M | recall@k (pgvector eval track) |
| ACT-R base-level activation (recency×frequency) | 2 | M | recall@k + helpfulness/MRR |
| Salient-fact extraction at ingest | 2 | M | recall@k + MRR (extracted vs blob) |
| MMR diversity re-rank | 2 | M | recall@k + ILAD/coverage (multi-target) |
| Associative linking + graph expansion (A-MEM/HippoRAG) | 2 | L | recall@k + MRR (multi-hop slice) |

### Guardrails — [guardrails.md](guardrails.md)  ·  6 Tier-1, 2 Tier-2, 4 rejected
| Feature | Tier | Effort | Eval metric moved |
|---|---|---|---|
| Unicode input canonicalization (de-obfuscation) | 1 | M | jailbreak/injection/PII recall & F1 |
| Corpus-derived signature pack (Aho-Corasick) | 1 | M | jailbreak/injection recall & F1 |
| ICAO 9303 MRZ passport detection | 1 | M | passport P/R/F1 (new label) |
| Decision-flip / negative-flip regression gate | 1 | S | Negative-Flip-Rate gate (new) |
| Red-team ASR eval split (JailbreakBench/HarmBench/garak) | 1 | M | per-category ASR ratchet (new) |
| CheckList metamorphic tests (MFT/INV/DIR) | 1 | M | invariance/directional failure rate (new) |
| Per-request canary-token leak detector (output) | 2 | S | `leak` label precision (new) |
| Native context-window PII validation → passport/name | 2 | M | passport/name P/R/F1 (new labels) |

---

## Two cross-cutting findings worth acting on

**1. The eval harnesses are the shared bottleneck — and the shared unlock.** Both RAG and Memory run their offline harness on a **deterministic mock/bag-of-words embedder** with no real vectors. That is *why* several genuinely promising techniques were rejected — HyDE, structure-aware chunking, `min_score` calibration (RAG), and honest ANN/`halfvec` recall (Memory) **cannot be measured** on the current harness, not because they lack merit. A one-time **real-embeddings / pgvector-backed eval track** unblocks all of them at once. For Guardrails, the equivalent is **wiring the already-authored 55-row golden suite** (currently unused) and adding the adversarial/red-team splits (B4–B6). *Invest in measurement first; it is what lets the rest be adopted with confidence.*

**2. The default path is already good — the wins are at the edges.** Because every service already ships the mainstream upgrades gated-off, the highest-value new work is (a) **de-obfuscation and coverage** the current rules miss (Guardrails B1–B3), (b) **cost/latency reductions** that are strictly net-positive (Memory embedding cache, `halfvec`, HNSW tuning), and (c) **regression-proofing** so quality can't silently erode (Guardrails B4–B6, RAG B1). None of these degrade the default path; most are measurable the day they land.

---

*Scope: the three Shared Core services only (`Shared Core/{rag,memory,guardrails}`). The product `cypherx-a1` and other platform services are out of scope. Effort: S ≈ ≤1 day · M ≈ a few days · L ≈ 1–2 weeks incl. eval work.*
