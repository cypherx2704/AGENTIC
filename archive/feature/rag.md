# RAG Service — Feature Dossier

> **Service:** `Shared Core/rag` (Phase 05 / WP09) · Python 3.12 / FastAPI · pgvector.
> **Purpose:** an accurate map of what retrieval quality is *already implemented*, followed by a **strictly-selected** set of new features worth building next. Every recommendation passed a five-point gate (not already built/gated · ≥2 independent credible sources · respects the cost/latency constraint · measurable by the `eval/` harness · concrete integration point). Candidates that failed are listed with reasons — the strictness is the point.
> **Method:** multi-agent web research (2024–2026 papers, framework/vendor docs) cross-checked against the actual source, then adversarially verified. **Date:** 2026-07-10.
>
> **Tiers.** **Tier 1 = no-regret:** no added cost/latency even when fully enabled (better algorithm, one-time ingest cost, or measurement). **Tier 2 = opt-in:** cost only behind an explicit flag with a guard + fail-soft, default path byte-identical.

---

## Part A — Features already implemented

The service is well past a scaffold. What ships today (code-verified):

| Capability | State | Where |
|---|---|---|
| Dense two-pass pgvector HNSW retrieval (index-friendly candidate CTE + `min_score` floor) | ✅ default | `services/store/pgvector.py`, `api/query.py` |
| Hybrid dense+lexical via **Reciprocal Rank Fusion** (RRF k=60; lexical leg = Postgres `tsvector`/GIN, same transaction) | ✅ coded, request-driven (`search_mode=hybrid`) | `pgvector.py:search_hybrid` |
| Sparse / lexical-only retrieval | ✅ coded (`search_mode=sparse`) | `pgvector.py` |
| **Cross-encoder reranking** via llms-gateway `POST /v1/rerank` | ✅ coded, **default off** (`rag_rerank_enabled=False`; per-query `rerank=true`); fail-soft | `services/rerank.py` |
| **Contextual-retrieval ingest** (Anthropic-style per-chunk situating context) | ✅ coded, **default off** (`rag_contextual_ingest=False`) | `services/contextual.py`, `services/ingest.py` |
| `fixed` + `sentence` chunking | ✅ | `services/chunking.py` |
| Per-KB ACLs, per-tenant quotas, transactional outbox usage metering, RLS | ✅ | `api/`, `db/` |
| Metadata filtering on query (`filters` → `metadata @> jsonb`, applied on both hybrid legs) | ✅ | `models/api.py`, `pgvector.py` |
| Pluggable `IVectorStore` interface | ✅ interface only — only `PgVectorAdapter` ships; dim locked to 1536 | `services/store/base.py` |
| Eval harness: **recall@k / nDCG@k / MRR** (dense/hybrid/hybrid+rerank) + `--assert-hybrid-ge-dense` CI gate | ✅ | `eval/run_eval.py` |

**Known gaps (facts):** no retrieval-result cache; no embedding cache; `min_score` default `0.0`; ingest parses only markdown/text (PDF bytes are decoded as UTF-8, never extracted); no query expansion; no document versioning; no semantic/recursive/structure-aware chunking.

> **Implication for "what to add":** the obvious retrieval upgrades (hybrid, rerank, contextual ingest) are already here — just gated off. New recommendations must be genuinely additive, not "flip a default." That single fact eliminated most naïve suggestions (see Part C).

---

## Part B — Recommended features

### Tier 1 (no-regret)

#### B1. Context-Precision (ID-based MAP) in the eval harness · effort **S**
**What.** Add RAGAS-style **ID-based Context Precision** (mean of Precision@k over the ranks where a golden-relevant chunk appears — pure arithmetic over the golden set's existing relevant-id lists, no LLM, no reference answer) to `run_eval.py`, reported per config at k=1,3,5 next to recall/nDCG/MRR. Optionally add a second CI gate: `hybrid+rerank` context-precision ≥ `hybrid`.

**Why high-impact.** The returned window *is* the caller's LLM context; its signal-to-noise ratio (how many distractors sit among the relevant chunks) drives downstream faithfulness. `recall@k` ignores where non-relevant items land and `nDCG@k` is ideal-normalized — neither isolates window precision. Without it, the harness can show rerank lifting MRR/nDCG while still flooding the window with near-duplicates. It is the one RAGAS retrieval metric not present, and 6 of 14 golden queries are multi-relevant, where MAP is genuinely distinct from MRR.

**Cost & latency.** Zero runtime cost. Lives entirely in the offline in-process harness (no Postgres/gateway/network). Query hot path untouched.

**Eval metric moved.** Adds Context-Precision@k; enables an optional second regression gate.

**Integration point.** `eval/run_eval.py`: add `_context_precision_at_k()` beside `_recall_at_k()`; extend `MetricRow`, `evaluate()`, `_print_table()`, the `--json` payload, and an optional `--assert-rerank-precision` flag. No changes outside `eval/`.

**Evidence.**
- Context Precision — RAGAS official docs (ID-based variant needs no LLM): https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/
- The Power of Noise: Redefining Retrieval for RAG Systems — arXiv 2401.14887 (why window signal-to-noise drives generation): https://arxiv.org/abs/2401.14887
- Contextual precision/recall explained — Milvus AI reference: https://milvus.io/ai-quick-reference/how-do-metrics-like-contextual-precision-and-contextual-recall-such-as-those-in-certain-rag-evaluation-frameworks-work-and-what-do-they-indicate-about-a-systems-performance

**Honest caveat.** MAP correlates with nDCG and collapses to MRR on single-relevant queries, so marginal signal is modest — but it is a genuine precision axis the harness lacks today, at literally zero cost/risk. This is the enabler for measuring everything else here.

---

### Tier 2 (opt-in; default path byte-identical)

#### B2. LLM query decomposition into sub-questions (multi-hop retrieval) · effort **M**
**What.** For compound/multi-hop queries, an LLM splits the query into ≤4 independent sub-questions; retrieve a pool per sub-question, union+dedup by `chunk_id`, then rerank the merged pool with the **already-coded** cross-encoder (`services/rerank.py`). Behind `rag_decompose_enabled` + per-query `decompose=true`, guarded exactly like today's `rerank` guard, fail-soft to single-query retrieval.

**Why high-impact.** Single-vector retrieval structurally cannot co-retrieve facts scattered across different chunks/documents — the dominant multi-hop failure mode. Decomposition issues one focused retrieval per fact, then recombines via fusion/rerank. The ACL SRW 2025 study reports **MRR@10 +36.7% and answer F1 +11.6%** over standard RAG on MultiHop-RAG + HotpotQA using exactly this decompose→retrieve-per-subq→rerank pattern — and the reranker it needs is already built here.

**Cost & latency.** Default (flag off or `decompose=false`): byte-identical. When enabled per-query: +1 serial LLM chat call (retrieval depends on it) + up to N−1 extra vector searches (parallelizable) + one rerank over the merged pool (cap the merged pool at `rerank_candidate_n`). ~2×+ latency on opted-in compound queries only.

**Eval metric moved.** MRR@10 / recall@10 on a new multi-hop slice — requires extending `KS` to include 10, a `decompose` entry in `_RANKERS`, and a small MultiHop-RAG-style golden slice whose supporting chunks live in separate docs.

**Integration point.** New `services/decompose.py` mirroring `services/contextual.py` (llms-gateway chat, mock-tolerant, fail-soft). `api/query.py`: guard + loop `store.search`/`search_hybrid` per sub-question + union/dedup + existing `_maybe_rerank`. `core/config.py`: `rag_decompose_enabled=False`, `decompose_max_subquestions=4`, `decompose_model="chat"`. `models/api.py`: `decompose: bool = False`. `eval/run_eval.py`: k=10 + decompose ranker + slice.

**Evidence.**
- Question Decomposition for Retrieval-Augmented Generation (Ammann, Golde, Akbik; ACL SRW 2025) — https://arxiv.org/abs/2507.00355
- Least-to-Most Prompting Enables Complex Reasoning (Zhou et al., ICLR 2023) — https://arxiv.org/abs/2205.10625 (mechanism support; corroborated by Self-Ask / IRCoT / DecomP)

**Honest caveat.** Only the direct SRW paper measures *retrieval* metrics; the reasoning-decomposition literature supports the mechanism, not the exact numbers. Strongest of the query-transformation family precisely because it reuses shipped rerank infra.

#### B3. Multi-query expansion fused via RRF (RAG-Fusion) · effort **M**
**What.** An LLM rewrites the query into several diverse paraphrases; retrieve for each; fuse the ranked lists with RRF (k=60). A **recall** lever for vocabulary-mismatch misses. Behind `rag_multiquery_enabled` + per-query `multi_query=true`, fail-soft to single-query.

**Why high-impact.** Fills the explicit "no query expansion" gap. LLM-generated expansions beat classical pseudo-relevance-feedback expansion on MS-MARCO/BEIR (Jagerman et al., Google). Pair with the gated cross-encoder reranker to restore top-k precision (the full RAG-Fusion recipe).

**Cost & latency.** Default (flag off / `multi_query=false`): byte-identical. Enabled: +1 synchronous LLM chat round-trip for variant generation + N−1 extra vector searches (parallelizable) + an app-level RRF fusion over N lists (negligible compute). Fail-soft to single-query.

**Eval metric moved.** recall@k (its intended lever); watch nDCG@5/MRR for precision dilution. Needs a `_rank_multiquery` entry and a crafted golden variant→relevance map (the mock embedder is non-semantic, so recall must be demonstrated with hand-authored variants).

**Integration point.** `api/query.py` (generate N variants via the `contextual.py` chat pattern; per-variant retrieval; **new** app-level RRF function); `rag_multiquery_enabled` in `core/config.py`; `multi_query: bool = False` in `models/api.py`.

**Evidence.**
- Query Expansion by Prompting LLMs (Jagerman et al., Google, 2023) — https://arxiv.org/abs/2305.03653
- RAG-Fusion: a New Take on RAG (Rackauckas, 2024) — https://arxiv.org/abs/2402.03367
- LangChain MultiQueryRetriever (framework docs) — https://reference.langchain.com/python/langchain-classic/retrievers/multi_query/MultiQueryRetriever
- Counter-evidence: ARAGOG (multi-query *decreased* precision) — https://arxiv.org/abs/2404.01037

**Honest caveat (important).** This is a **recall** lever, not a precision lever — the proposer's own ARAGOG citation shows plain multi-query can hurt precision. Only recommend it paired with the reranker, and gate it to recall-sensitive use. The "reuses the existing RRF" claim is overstated: the shipped RRF is SQL-internal two-leg fusion, so a small new N-list fusion is required. Lower priority than B2.

---

## Part C — Rejected candidates (strictness applied)

Nine researched candidates were cut. Most failed because the offline harness runs on a deterministic hash mock embedder that cannot model the semantic effect being sold, or because they silently duplicate an already-gated feature.

| Candidate | Failing gate | Reason |
|---|---|---|
| Structure-aware Markdown chunking (heading-split + header-path metadata) | Measurability + evidence | Harness scores by fixed `chunk_id` identity; changing chunk boundaries breaks the mapping, so strategies can't be compared without a new corpus + span-containment matching (Medium rework), and the mock embedder is semantically uninformative. The strongest cited numbers are for *LLM* semantic chunking, not the cheap heading-split. |
| Small-to-big / parent-document retrieval | Evidence | Its own primary source (LlamaIndex AutoMergingRetriever) reports near-identical correctness/faithfulness vs the base retriever (52.5% vs 47.5% preference) — marginal, on the exact metric family the harness measures. |
| Deterministic metadata enrichment (structural prepend + metadata pre-filter) | **Already built** | Metadata filtering is shipped and tested (`QueryRequest.filters`, `metadata @>` on both hybrid legs); the "structural prepend before embedding" duplicates the gated contextual-ingest path. |
| Raise `hnsw.ef_search` + iterative index scans | **Already built** + measurability | `ef_search` is already default-on (100, cap 500) and wired per-query; the ACL-decimation premise is false (ACL is a pre-search 403 gate); the fake DB swallows `SET LOCAL hnsw.ef_search`, so it's inert in the harness. |
| MMR / diversity de-duplication of results | Evidence | ARAGOG (its own RAG-empirical source) finds MMR gives "no notable advantage over naive RAG"; standard nDCG@k can even *regress* under per-chunk qrels. |
| Hybrid (RRF) as the **default** `search_mode` | **Gated-feature duplicate** + cost polarity | Only flips a config default to enable an already-gated feature; makes the costly path the default (violates both tiers). |
| HyDE (hypothetical document embeddings) | Measurability | The benefit is a real-embedding phenomenon; under a token-hash mock it cannot appear, and HyDE hurts on numeric/exact-fact KBs. A meaningful test needs a real-embedder OOD regime (L-effort), not a small extension. |
| Step-back (abstraction) query generation | Evidence | Only source with numbers (Zheng et al., ICLR 2024) reports QA/answer-accuracy gains, not retrieval-metric movement — off-target for this harness. |
| No-answer slice + calibrated non-zero `min_score` default | Measurability | The harness's hash-cosine distribution is degenerate for abstention tuning; a floor calibrated on mock cosines wouldn't transfer to the real embedder, and raising the default changes retrieval for every caller (not byte-identical). |

**Cross-cutting lesson.** Several genuinely promising ideas (structure-aware chunking, HyDE, min_score calibration) are **blocked by the eval harness, not by merit.** They become viable the moment a real-embeddings eval path exists — which is why **B1 (measurement) is the highest-leverage first step**: it, plus a real-embedder harness track, unlocks honest evaluation of the rest.
