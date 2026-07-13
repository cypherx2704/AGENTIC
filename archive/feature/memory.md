# Memory Service — Feature Dossier

> **Service:** `Shared Core/memory` (Phase 06 / WP10) · Python 3.12 / FastAPI · pgvector.
> **Purpose:** an accurate map of what's *already implemented*, followed by a **strictly-selected** set of new features worth building next. Every recommendation passed a five-point gate (not already built/gated · ≥2 independent credible sources · respects the cost/latency constraint · measurable by `eval/` · concrete integration point). Rejected candidates are listed with reasons.
> **Method:** multi-agent web research (2024–2026 papers, framework/vendor docs) cross-checked against the actual source, then adversarially verified. **Date:** 2026-07-10.
>
> **Tiers.** **Tier 1 = no-regret:** no added cost/latency even when fully enabled (better algorithm, one-time cost, measurement). **Tier 2 = opt-in:** cost only behind a flag with a guard + fail-soft, default path byte-identical.

---

## Part A — Features already implemented

| Capability | State | Where |
|---|---|---|
| Store / vector-search / by-id CRUD / sessions / GDPR wipe | ✅ | `api/`, `services/pg_repository.py` |
| Dense two-pass pgvector HNSW search (oversample ×4, ×8 with scoring) | ✅ | `pg_repository.py:search` |
| Dedup **bump-only** at ≥0.95 cosine (nearest neighbour at insert) | ✅ | `pg_repository.py` |
| Idempotency-Key short-circuit **before** embedding; TTL sweep | ✅ | `api/memories.py` |
| **Composite scoring** (Generative-Agents recency+importance+relevance; pure-Python re-rank of fetched candidates) | ✅ coded, **default off** (`memory_scoring_enabled=False`) | `services/scoring.py` |
| Heuristic write-time importance (always on) + **LLM importance grader** | ✅ heuristic; grader is a **default-off skeleton** (`memory_importance_llm_enabled=False`, returns `None`) | `api/memories.py`, `services/scoring.py` |
| **Contradiction / temporal-validity supersession** (real heuristic) | ✅ coded, **default off** (`memory_contradiction_enabled=False`) | `services/contradiction.py` |
| **Consolidation / forgetting** job | ⚠️ **default-off skeleton** — soft-deletes but does **not** insert summary memories (`memory_consolidation_enabled=False`) | `services/consolidation.py` |
| Usage events, quotas, RLS, transactional outbox | ✅ (usage events **default on**) | `db/`, `services/quota.py` |
| Eval harness: **recall@k + helpfulness (MRR)**, cosine vs composite, no-regression CI guard | ✅ | `eval/run_eval.py` |

**Known gaps / facts:** **no embedding cache** (every store *and* every search re-embeds fresh); `last_accessed_at`/`last_retrieved_at` written **inline on every read** (a write on the read hot path); HNSW index has **no explicit `m`/`ef_construction`** and `ef_search` is the pgvector default 40; 2-value scope (`principal_only|tenant_shared`); no auto-extraction, no summarisation, no working memory, no re-embed job, no `user_scope_acl`, no async `last_accessed` batching.

> **Implication for "what to add":** the composite scorer, contradiction detector, and consolidation skeleton already exist gated-off — recommendations must be genuinely new mechanisms, not those.

---

## Part B — Recommended features

### Tier 1 (no-regret)

#### B1. `halfvec` (16-bit) HNSW index — 2× smaller index at ~identical recall · effort **M**
**What.** Store/index the 1536-dim embedding as `halfvec` via an HNSW **expression index** `USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops)`, casting the query the same way. (Optional aggressive tier: a `binary_quantize()::bit(1536)` first pass + full-precision rerank — this part is Tier 2.)

**Why high-impact.** Vectors dominate storage and index RAM, and keeping the HNSW index memory-resident is the primary driver of pgvector query latency. `halfvec` halves vector storage/index size at 1536-dim with **negligible recall change** (measured ~identical recall) and equal-or-slightly-better query latency — a near-free memory-residency win.

**Cost & latency.** Tier 1: one-time index build/backfill; per-query adds only a negligible `::halfvec` cast while scanning a ~2× smaller index (equal-or-lower latency, better RAM residency). Default path byte-identical behind `memory_vector_quantization=off`.

**Eval metric moved.** recall@k (quantization is lossy). Realistic offline check: round the golden vectors to float16 (numpy) before cosine and measure the delta. *Caveat:* the in-memory BoW harness can't measure true HNSW ANN recall at a given `ef_search`; the binary+rerank "99% recall" claim needs a pgvector-backed eval path (not a small extension).

**Integration point.** New additive migration adding the `halfvec` expression HNSW index on `memory.memory_vectors_1536`; cast to `::halfvec(1536)` in the `search()` ANN CTE and the `store()` dedup neighbour query in `pg_repository.py`; `memory_vector_quantization` flag (off|halfvec|binary_rerank) in `core/config.py`.

**Evidence.**
- Scalar & binary quantization for pgvector — Jonathan Katz (pgvector maintainer; halfvec 2×, ~identical recall): https://jkatz05.com/post/postgres/pgvector-scalar-binary-quantization/
- Don't use `vector`, use `halfvec` — Neon engineering: https://neon.com/blog/dont-use-vector-use-halvec-instead-and-save-50-of-your-storage-cost
- pgvector README (`halfvec`, `binary_quantize()`, expression indexes): https://github.com/pgvector/pgvector

**Honest caveat.** Peripheral overclaims dropped: Contract-19 `storage_bytes` meters *content* bytes not vectors (no quota relief), and an expression index halves index size only (the base `vector(1536)` column is unchanged). The binary+rerank escalation is the weaker part (single strong source, hard to measure offline) and is kept as an opt-in Tier-2 escalation.

---

### Tier 2 (opt-in; default path byte-identical)

Ordered by leverage-per-effort.

#### B2. Content-hash embedding cache (Valkey) · effort **M** — *highest no-regret value in this tier*
**What.** Wrap the embedding client to key a small store on `hash(model + dim + normalized_text) → vector`. Hit ⇒ return cached vector; miss ⇒ embed once and write back. Identical text under the same model yields an identical vector, so the cache is **exact** (no semantic fuzz).

**Why high-impact.** Today `embeddings.py` re-embeds on **every store and every search**. The Idempotency-Key short-circuit only helps exact *write* replays — it does nothing for repeated *query* text (agent retry / pagination / tool loops) or identical content stored via different requests, and nothing for a future re-embed job. An exact cache replaces a synchronous gateway round-trip (network + model inference — the dominant $/latency on the hot path) with a sub-ms Valkey GET. Published exact-match hit rates run ~60–80% in repetitive production streams. Valkey already runs here.

**Cost & latency.** Default off ⇒ byte-identical. Enabled: a HIT is a net reduction at both ingest and query time; a MISS adds a GET+SET around the normal embed (why it's Tier 2, not Tier 1). Fail-open to a normal embed on Valkey outage. **Correctness guard:** the key must namespace `model + dim` (+ TTL) so a model/dim change never serves a stale vector.

**Eval metric moved.** Cost: `embed_calls_total` drops on repeated identical text (assert via a unit test mirroring `tests/test_dedup_and_idempotency.py`). recall@k/MRR unchanged (identical vectors), guarded by the existing no-regression CI check.

**Integration point.** `services/embeddings.py`: wrap `embed_many`/`embed_one` with per-text hash lookup + batch partial-hit handling; back with `db/valkey.py`; `memory_embedding_cache_enabled` (default off) + TTL in `core/config.py`.

**Evidence.**
- CacheBackedEmbeddings — LangChain docs (exact hash-keyed pattern): https://python.langchain.com/api_reference/langchain/embeddings/langchain.embeddings.cache.CacheBackedEmbeddings.html
- GPT Semantic Cache — arXiv 2411.05276 (Redis embedding/query cache; 61–69% measured hit rates): https://arxiv.org/html/2411.05276v1
- Caching Strategies for RAG (embeddings & responses) — APXML: https://apxml.com/courses/optimizing-rag-for-production/chapter-4-end-to-end-rag-performance/caching-strategies-rag

**Honest caveat.** "Byte-identical to a fresh embed" slightly overstates it (GPU/batching nondeterminism) — but the cached vector is a valid, self-consistent embedding of the exact text, and the recall@k CI guard catches any drift.

#### B3. Explicit HNSW build params (`m`/`ef_construction`) + query-time `ef_search` · effort **M** — *fixes a real recall-truncation defect*
**What.** Build the index `WITH (m=16..32, ef_construction=128..256)` instead of the unspecified defaults, and issue `SET LOCAL hnsw.ef_search` before the ANN CTE so the first-pass candidate list is large enough for the intended oversample.

**Why high-impact.** `search()` pulls a candidate window of `top_k × 4` (×8 when scoring, up to 400), but with the default `hnsw.ef_search = 40` the HNSW scan returns **at most 40 candidates regardless of the LIMIT** — so the oversample is silently capped at 40, and the composite re-rank + visibility/type/tag filters run on a truncated pool. Because `search_top_k_max = 50 > 40`, the ANN pass can't even fill a full `top_k`. Raising `ef_search` toward the window makes the oversample real and lifts recall@k; higher `ef_construction`/`m` improves graph quality at the same `ef_search`.

**Cost & latency.** `m`/`ef_construction` = one-time index-build cost, zero query cost (Tier-1-flavoured). `ef_search` is a per-query GUC that trades latency for recall — gated behind `memory_hnsw_ef_search`, default preserving today's behaviour, so the default path stays byte-identical (⇒ Tier 2 overall).

**Eval metric moved.** recall@k — but requires a **named** harness extension: seed a real pgvector table, run the golden set through `PgMemoryRepository`, and sweep `ef_search` (40 vs 200 vs 400). The current in-memory harness can't see the ANN cap.

**Integration point.** New additive migration rebuilding `memory.idx_memory_vectors_hnsw WITH (m, ef_construction)`; emit `SET LOCAL hnsw.ef_search` in `search()._txn` and `store()._txn` in `pg_repository.py`; `memory_hnsw_m`/`_ef_construction`/`_ef_search` flags in `core/config.py`; the eval probe.

**Evidence.**
- pgvector README (defaults m=16 / ef_construction=64 / ef_search=40; `ef_search` bounds returned rows regardless of SQL LIMIT): https://github.com/pgvector/pgvector
- Running pgvector in production on Amazon Aurora PostgreSQL — AWS Database Blog: https://aws.amazon.com/blogs/database/running-pgvector-in-production-on-amazon-aurora-postgresql/
- The 150x pgvector speedup — Jonathan Katz: https://jkatz05.com/post/postgres/pgvector-performance-150x-speedup/

**Honest caveat.** The recall gap is real and code-verified, but demonstrating it requires the pgvector-backed eval track (also needed by B1 and by RAG's blocked candidates) — build that harness track once, reuse it three times.

#### B4. ACT-R base-level activation (recency × frequency, power-law decay) · effort **M**
**What.** Augment the composite's exponential recency term with ACT-R base-level activation, which fuses **how recently *and* how often** a memory was retrieved into one activation with power-law decay, via the cheap Petrov O(1) approximation `B ≈ ln(n) − d·ln(age)`. Adds the retrieval-frequency reinforcement the current composite entirely lacks.

**Why high-impact.** `composite_score` today uses only `last_retrieved_at` → exponential recency; it has **no frequency term**, so a fact retrieved 50 times and one retrieved once decay identically at equal age. Anderson & Schooler showed real-environment retrieval need-odds follow a power law of recency *and* frequency (exponential over-forgets the mid-tail). This makes durable, oft-used facts outrank stale one-offs at equal cosine — exactly the recency-vs-relevance tie the eval is built around.

**Cost & latency.** Effectively free. The composite re-rank is already a pure-Python sort over fetched candidates; adding `B` is O(1) per candidate — no extra DB read, no embed. The read-path bump `UPDATE ... last_accessed_at=NOW()` already runs on every search; folding in `access_count = access_count + 1` is one extra SET column on an existing statement. One additive migration column. Tier 2 only on strictness (ranking change behind a flag).

**Eval metric moved.** recall@k + helpfulness/MRR via a named extension: add `access_count` to `golden_memories.json`, seed it, wire `base_level_activation` into `composite_score`. The harness's top_k=1 cosine-tie design is exactly where a high-frequency target should overtake a stale one-off.

**Integration point.** `services/scoring.py`: new `base_level_activation()` wired into `composite_score` behind `memory_scoring_decay='power_actr'` + `memory_scoring_frequency_weight`. `pg_repository.py`: add `access_count = access_count + 1` to the inline read-path UPDATE and to SELECT column lists. New additive migration `ADD COLUMN access_count BIGINT NOT NULL DEFAULT 0`. Flags in `core/config.py`.

**Evidence.**
- Schooler & Anderson (2017), The Adaptive Nature of Memory (ACT-R; power-law recency+frequency) — http://act-r.psy.cmu.edu/wordpress/wp-content/uploads/2021/07/SchoolerAnderson2017.pdf
- Petrov (2006), Efficient Approximation of the Base-Level Learning Equation (O(1) form) — http://alexpetrov.com/pub/iccm06/PetrovICCM06.pdf
- Introducing Memory Decay in Mem0 (production recency/access-reinforced rerank) — https://mem0.ai/blog/introducing-memory-decay-in-mem0

**Honest caveat.** The uplift is demonstrated on golden data you add, so the no-regression CI guard is the load-bearing check.

#### B5. Salient-fact extraction at ingest (Mem0 / LangMem atomic-fact decomposition) · effort **M**
**What.** Behind a flag, one LLM pass at write time decomposes incoming content into atomic, self-contained facts, each stored as its own memory row with its own embedding — instead of embedding a multi-fact blob as one averaged vector. **Extraction only** — deliberately *not* the gated contradiction reconciliation, *not* the gated consolidation summary.

**Why high-impact.** The store path embeds `body.content` verbatim, so a paragraph of several facts collapses into one vector that averages unrelated topics; a query about any single fact matches it weakly and recall@k suffers. Atomic facts give each fact a focused, un-diluted embedding — the mechanism Mem0 and LangMem credit for their accuracy gains. "Auto-extraction" is an explicit baseline gap.

**Cost & latency.** Ingest-only, flag-gated: one extraction call + N per-fact embeds + N writes per request when enabled. Read/search hot path untouched. Default off ⇒ byte-identical. Fail-soft to raw-content storage (mirrors the `_grade_importance_llm` skeleton). Idempotency short-circuit stays before extraction.

**Eval metric moved.** recall@k (+ MRR): the averaging-dilution effect is deterministically demonstrable in the offline BoW harness (a multi-fact blob's normalized BoW vector matches a single-fact query more weakly than the isolated fact does). Add multi-fact source items with per-fact expected ids; compare "extracted" vs "raw blob" variants.

**Integration point.** New `services/extraction.py` (split decision + llms-gateway seam mirroring `_grade_importance_llm`); invoked in `api/memories.py:store_memory` after the content cap + idempotency short-circuit and before `embed_one`, looping `repository.new_memory` + `repo.store`. `memory_extraction_enabled` (default False) in `core/config.py`.

**Evidence.**
- Mem0: Production-Ready AI Agents with Scalable Long-Term Memory (Chhikara et al., arXiv 2504.19413) — https://arxiv.org/abs/2504.19413
- LangMem — How to Extract Semantic Memories (LangChain docs) — https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/

**Honest caveat.** Mem0's headline magnitudes (61.4%, +26%, ~90% token savings) come from the **full** extract+consolidate+retrieve pipeline, and the token/latency savings are a different mechanism (not sending full context at query time) — the *direction* is well-supported but the exact numbers are over-attributed to extraction alone. **Design cost to price in:** fanning one request into N rows forces decisions on the 201 response shape, idempotency replay body, per-fact dedup-bump, and per-fact vs aggregate usage metering.

#### B6. MMR diversity re-rank of the candidate window · effort **M**
**What.** A query-time greedy re-rank of the already-fetched oversampled window: pick the memory maximizing `λ·relevance − (1−λ)·max(cosine to already-selected)`, so the returned top_k covers distinct facets instead of k near-paraphrases. Pure post-processing over vectors already in hand.

**Why high-impact.** The two-pass ANN oversamples but the final top_k is picked by pure cosine (or the per-item composite) — neither has any cross-item redundancy term. Write-time dedup only bump-merges at ≥0.95 against the single nearest neighbour, so sub-0.95 paraphrases and the same fact stored across sessions accumulate and crowd top_k, pushing out the complementary memory the agent needs. MMR spends the fixed top_k budget on distinct information.

**Cost & latency.** Query-time only, no extra DB round-trip, no embed call. Needs candidate vectors resident (add `v.embedding` to the ANN CTE **conditionally on the flag** to keep the default byte-identical). Implement with numpy (pure-Python at window=400 is seconds; vectorized is sub-ms–low-ms). Default off ⇒ byte-identical. Fail-soft to existing order.

**Eval metric moved.** recall@k on multi-target queries + an Intra-List Average Distance (ILAD) diversity column, via a redundancy-heavy multi-target golden fixture at top_k>1 (current harness is top_k=1, where MMR == cosine). recall@k stays the must-not-regress guard.

**Integration point.** New `mmr_rerank()` in `services/scoring.py`; called at the read re-rank step in `pg_repository.py:search()` and the in-memory repo; `memory_mmr_enabled` (default false) + `memory_mmr_lambda` in `core/config.py`.

**Evidence.**
- The Use of MMR for Reranking & Summaries (Carbonell & Goldstein, SIGIR 1998) — https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf
- AdaGReS: Adaptive Greedy Context Selection via Redundancy-Aware Scoring (arXiv 2512.25052; redundancy-aware selection improves end-to-end answer quality) — https://arxiv.org/pdf/2512.25052
- MMR vector search — Google Cloud (independent vendor endorsement) — https://docs.cloud.google.com/bigtable/docs/mmr-vector-search

**Honest caveat.** The benefit needs top_k>1 + a redundancy-heavy fixture to be visible; ILAD measures the mechanism MMR optimizes (near-tautological), so pair it with a coverage@k metric over multi-target queries to prove downstream quality. Qualified pass, not a strong one.

#### B7. Associative memory linking + graph-expansion retrieval (A-MEM / HippoRAG) · effort **L**
**What.** At ingest (flagged), generate explicit links from each new memory to its nearest neighbours (Zettelkasten-style); at retrieval, after the two-pass ANN, do a bounded **1-hop, embedding-free** link expansion (or a Personalized-PageRank pass over the small fetched subgraph) to surface associated memories cosine missed. Link **construction + associative retrieval only** — not the gated consolidation summary, not the gated supersession.

**Why high-impact.** Single-shot cosine cannot retrieve a memory relevant by *association* but not close to the query vector — the classic multi-hop miss (query mentions A; the needed fact lives under B, which A links to). A-MEM and HippoRAG report large multi-hop gains (HippoRAG: up to +20% on multi-hop QA, 6–13× faster than iterative retrieval; A-MEM improves LOCOMO/DialSim); Mem0's graph variant corroborates. The retrieval-time walk is an embedding-free DB join over candidates the ANN already returned.

**Cost & latency.** Default off ⇒ byte-identical. Ingest (on): one extra k-NN neighbour query (partly reusable from the dedup fetch) + one link-decision LLM call per store — one-time, off the read path, fail-soft. Query (on): a bounded 1-hop join over `memory_links` for the top-k IDs + a fetch of linked rows — **one extra DB round-trip, no extra vector/embed call**, fail-soft to the vector-only set.

**Eval metric moved.** recall@k + MRR via a named multi-hop extension: golden queries whose expected memory is reachable **only** through a link, plus the link edges; measure recall with vs without expansion. Guard: expansion must not regress single-hop recall@k.

**Integration point.** New RLS-scoped `memory.memory_links` edge table (mirror the tenant/RLS predicate of `memory.memories`); new `services/linking.py` (llms-gateway seam); `pg_repository.py` `store()` writes edges and `search()` adds the bounded expansion after the oversample CTE; `memory_linking_enabled` in `core/config.py`; extend the in-memory repo + golden set so the offline harness can measure it.

**Evidence.**
- A-MEM: Agentic Memory for LLM Agents (Xu et al., arXiv 2502.12110; NeurIPS 2025) — https://arxiv.org/abs/2502.12110
- HippoRAG: Neurobiologically Inspired Long-Term Memory (Gutierrez et al., NeurIPS 2024) — https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ddc001d07ca4f319af96a3024f6dbd1-Abstract-Conference.html
- Mem0 graph variant (Mem0g) — arXiv 2504.19413 — https://arxiv.org/abs/2504.19413

**Honest caveat.** The biggest lift but the most work (L): the eval extension is non-trivial (edges in the golden set + expansion in the in-memory repo), and the retrieval walk is a real extra DB round-trip — both opt-in, so within Tier 2. Sequence it after the cheaper wins.

---

## Part C — Rejected candidates (strictness applied)

| Candidate | Failing gate | Reason |
|---|---|---|
| RRF combiner for the composite signals | Evidence + impact | RRF is established for fusing heterogeneous *retriever* lists, not for fusing a relevance score with recency/importance metadata priors; it gives recency an equal rank-vote so a fresh off-topic distractor still competes — doesn't fix the named failure. An alternate fusion of the same three signals; marginal, unproven. |
| pgvector 0.8 `hnsw.iterative_scan` for the filtered two-pass | Measurability + inert integration | PASS 1 is an *unfiltered* ANN CTE; iterative scan only helps when predicates on the same scan prune below the LIMIT. The filters run in PASS 2 on a separate table, so the GUC changes nothing without denormalizing filter columns onto the vector table (a large redesign). Also unmeasurable in the in-memory harness. |
| Lost-in-the-Middle U-shape context ordering | Measurability + regime | The harness has no gold answers and is LLM-free, so order-only changes are invisible to recall/MRR; the effect is realized at prompt-assembly time (in xAgent/cypherx-a1), so returning a non-monotonic array from a relevance-ranked API is a footgun for other consumers. |
| Session-aware conversational query rewrite | Integration premise false | The `Session` object holds only id/tenant/principal/title/metadata — **no turns/transcript**. The rewriter has no referent context; implementing it first requires building turn storage (substantial plumbing), so the tier-2/effort claims collapse. |

**Cross-cutting lesson.** As with RAG, several candidates (B1 binary tier, B3, and multiple rejects) are limited by the **in-memory BoW eval harness**. A one-time **pgvector-backed eval track** is the shared unlock — build it once; it serves halfvec recall, HNSW `ef_search` sweeps, and honest ANN measurement.
