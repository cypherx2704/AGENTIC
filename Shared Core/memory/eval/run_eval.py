"""Offline eval harness: cosine vs composite retrieval scoring (recall@k + helpfulness).

Loads ``golden_memories.json``, stores it in the in-memory repository with the golden
``importance`` + ``age_days``, then runs each query under BOTH rankings (pure cosine and
the Generative-Agents composite) and reports recall@k + a helpfulness proxy. Exits non-zero
if composite REGRESSES recall@k vs cosine on the golden set, so it doubles as a CI guard.

Uses a deterministic bag-of-words embedder (no network, no heavy model) so semantically
related text has a high cosine — that makes recall@k meaningful and keeps the harness
runnable in the offline test environment exactly like the rest of the suite.

Run:
    ./.venv/Scripts/python.exe eval/run_eval.py
    ./.venv/Scripts/python.exe eval/run_eval.py --json
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make ``src`` importable when run directly from the repo root.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from memory_service.services.repository import (  # noqa: E402
    InMemoryRepository,
    StoredMemory,
    new_memory,
)
from memory_service.services.scoring import ScoringWeights  # noqa: E402

_TENANT = "00000000-0000-0000-0000-0000000000aa"
_PTYPE = "agent"
_PID = "eval-agent"
# top_k=1 makes ORDER decisive: when a stale distractor ties the target on cosine, only the
# correctly-ranked memory lands in the top-1, so recall@1 directly measures the re-rank win.
_TOP_K = 1
# A 60-day recency half-life keeps month-old high-importance facts competitive while still
# decaying year-old stale memories — the regime where composite helps without over-forgetting.
_RECENCY_HALF_LIFE_S = 60 * 24 * 3600.0
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _vocab(memories: list[dict], queries: list[dict]) -> list[str]:
    """Stable sorted vocabulary across all golden text (deterministic dimension order)."""
    words: set[str] = set()
    for m in memories:
        words.update(_TOKEN_RE.findall(m["content"].lower()))
    for q in queries:
        words.update(_TOKEN_RE.findall(q["query"].lower()))
    return sorted(words)


def _bow_vector(text: str, vocab: list[str]) -> list[float]:
    """L2-normalized bag-of-words vector over ``vocab`` (related text -> high cosine)."""
    counts: dict[str, int] = dict.fromkeys(vocab, 0)
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in counts:
            counts[tok] += 1
    vec = [float(counts[w]) for w in vocab]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class Metrics:
    recall_at_k: float
    helpfulness: float  # mean normalized rank of the expected memory (1.0 best)
    hits: int
    total: int


async def _load_repo(memories: list[dict], vocab: list[str]) -> InMemoryRepository:
    repo = InMemoryRepository()
    now = datetime.now(UTC)
    for spec in memories:
        mem: StoredMemory = new_memory(
            tenant_id=_TENANT, principal_type=_PTYPE, principal_id=_PID, scope="principal_only",
            type=spec.get("type", "note"), tags=[], content=spec["content"], metadata={},
            vector=_bow_vector(spec["content"], vocab), session_id=None, ttl_seconds=None,
            importance_score=float(spec["importance"]),
        )
        # Force the golden id + age so recency/importance can diverge from cosine.
        mem.id = spec["id"]
        created = now - timedelta(days=float(spec.get("age_days", 0)))
        mem.created_at = created
        mem.last_accessed_at = created
        mem.last_retrieved_at = created
        mem.access_count = int(spec.get("access_count", 0))  # B4 ACT-R frequency input
        repo._memories[mem.id] = mem  # noqa: SLF001 — eval harness, direct seed is fine
    return repo


async def _eval(
    repo: InMemoryRepository, queries: list[dict], vocab: list[str], *, scoring_enabled: bool
) -> Metrics:
    hits = 0
    rank_scores: list[float] = []
    weights = ScoringWeights(recency_half_life_seconds=_RECENCY_HALF_LIFE_S)
    for q in queries:
        qv = _bow_vector(q["query"], vocab)
        expected = q["expected"]
        # recall@k uses the configured top_k window.
        top = await repo.search(
            tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID, query_vector=qv,
            top_k=_TOP_K, type_filter=None, tags_filter=None, include_shared=True,
            user_scope_visibility="isolated", scoring_enabled=scoring_enabled,
            scoring_weights=weights, current_only=False,
        )
        if expected in [m.id for m in top]:
            hits += 1
        # helpfulness uses the FULL ranking so it captures rank improvements below top_k.
        full = await repo.search(
            tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID, query_vector=qv,
            top_k=1000, type_filter=None, tags_filter=None, include_shared=True,
            user_scope_visibility="isolated", scoring_enabled=scoring_enabled,
            scoring_weights=weights, current_only=False,
        )
        full_ids = [m.id for m in full]
        rank_scores.append(1.0 / (full_ids.index(expected) + 1) if expected in full_ids else 0.0)
    total = len(queries)
    return Metrics(
        recall_at_k=hits / total if total else 0.0,
        helpfulness=sum(rank_scores) / total if total else 0.0,
        hits=hits,
        total=total,
    )


def _seed(
    repo: InMemoryRepository, specs: list[dict], vocab: list[str], *, now: datetime
) -> None:
    """Seed ``specs`` into ``repo`` with golden id/age/importance/access_count (BoW vectors)."""
    for spec in specs:
        mem = new_memory(
            tenant_id=_TENANT, principal_type=_PTYPE, principal_id=_PID, scope="principal_only",
            type=spec.get("type", "note"), tags=[], content=spec["content"], metadata={},
            vector=_bow_vector(spec["content"], vocab), session_id=None, ttl_seconds=None,
            importance_score=float(spec.get("importance", 0.5)),
        )
        mem.id = spec["id"]
        created = now - timedelta(days=float(spec.get("age_days", 0)))
        mem.created_at = created
        mem.last_accessed_at = created
        mem.last_retrieved_at = created
        mem.access_count = int(spec.get("access_count", 0))
        repo._memories[mem.id] = mem  # noqa: SLF001 — eval harness direct seed
    return None


# ── B1: halfvec (float16) recall-delta probe ─────────────────────────────────────────
def float16_recall_delta(data: dict) -> dict:
    """Round the golden BoW vectors to numpy float16 and measure the recall@1 delta.

    A realistic offline stand-in for the halfvec (16-bit) index: quantize both memory and
    query vectors to float16, recompute cosine top-1, and compare against the float32
    baseline. Uses SELF-RETRIEVAL (query each memory by its own content, expecting itself) so
    the baseline recall is meaningful (~1.0); the halfvec claim is that quantizing to 16-bit
    leaves recall unchanged, i.e. delta ≈ 0. (True HNSW ANN recall at a given ef_search still
    needs a pgvector-backed eval track — the in-memory harness runs no SQL.)
    """
    import numpy as np

    memories = data["memories"]
    probes = [{"query": m["content"], "expected": m["id"]} for m in memories]
    vocab = _vocab(memories, probes)
    mvecs = {m["id"]: np.asarray(_bow_vector(m["content"], vocab), dtype=np.float32) for m in memories}

    def _recall(dtype) -> float:  # type: ignore[no-untyped-def]
        hits = 0
        for q in probes:
            qv = np.asarray(_bow_vector(q["query"], vocab), dtype=np.float32).astype(dtype).astype(np.float32)
            best_id, best = None, -2.0
            for mid, mv in mvecs.items():
                v = mv.astype(dtype).astype(np.float32)
                denom = (np.linalg.norm(v) * np.linalg.norm(qv)) or 1.0
                cos = float(v @ qv / denom)
                if cos > best:
                    best, best_id = cos, mid
            if best_id == q["expected"]:
                hits += 1
        return hits / len(probes) if probes else 0.0

    r32 = _recall(np.float32)
    r16 = _recall(np.float16)
    return {"recall_f32": r32, "recall_f16": r16, "delta": r16 - r32}


# ── B4: ACT-R recency x frequency on a cosine/recency tie ─────────────────────────────
async def run_frequency_actr(data: dict) -> dict:
    """Two identical-content/recency/importance memories differing ONLY in access_count.

    Pure cosine (and the exponential composite) can only tie-break by input order; ACT-R
    (power_actr) must promote the high-frequency memory. Returns recall@1 for each mode.
    """
    mems, queries = data["frequency_memories"], data["frequency_queries"]
    vocab = _vocab(mems, queries)
    now = datetime.now(UTC)

    async def _recall(weights: ScoringWeights | None) -> float:
        repo = InMemoryRepository()
        _seed(repo, mems, vocab, now=now)
        hits = 0
        for q in queries:
            top = await repo.search(
                tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID,
                query_vector=_bow_vector(q["query"], vocab), top_k=1, type_filter=None,
                tags_filter=None, include_shared=True, user_scope_visibility="isolated",
                scoring_enabled=weights is not None, scoring_weights=weights, current_only=False,
            )
            if top and top[0].id == q["expected"]:
                hits += 1
        return hits / len(queries) if queries else 0.0

    actr = ScoringWeights(decay="power_actr", frequency_weight=1.0, actr_decay_d=0.5)
    return {
        "cosine_recall": await _recall(None),
        "exponential_recall": await _recall(ScoringWeights(recency_half_life_seconds=_RECENCY_HALF_LIFE_S)),
        "actr_recall": await _recall(actr),
    }


# ── B5: extracted atomic facts vs one averaged multi-fact blob ────────────────────────
async def run_extraction(data: dict) -> dict:
    """Averaging dilution: a single-fact query matches the isolated fact more strongly than
    the multi-fact blob that averages it with unrelated facts.

    For every fact-query compares cosine(query, isolated fact) vs cosine(query, whole blob),
    and — in a shared repo holding BOTH the blob and all isolated facts — checks that the
    top-1 for the query is the focused fact, not the diluted blob. Extraction wins because a
    focused embedding is more retrievable (the mechanism Mem0/LangMem credit for accuracy).
    """
    from memory_service.services.extraction import extract_facts

    total = 0
    blob_sim_sum = 0.0
    ext_sim_sum = 0.0
    ext_top1 = 0  # queries whose top-1 (blob + facts in one repo) is the focused fact
    now = datetime.now(UTC)
    for item in data["extraction_items"]:
        facts = extract_facts(item["source"], max_facts=32)
        texts = [item["source"], *facts] + [q["query"] for q in item["queries"]]
        vocab = sorted({t for txt in texts for t in _TOKEN_RE.findall(txt.lower())})
        blob_vec = _bow_vector(item["source"], vocab)
        fact_vecs = [_bow_vector(f, vocab) for f in facts]

        repo = InMemoryRepository()
        _seed(repo, [{"id": item["id"], "content": item["source"]}], vocab, now=now)
        _seed(repo, [{"id": f"{item['id']}-f{i}", "content": f} for i, f in enumerate(facts)],
              vocab, now=now)

        for q in item["queries"]:
            total += 1
            qv = _bow_vector(q["query"], vocab)
            fi = q["expected_fact_index"]
            blob_sim_sum += _cos(qv, blob_vec)
            ext_sim_sum += _cos(qv, fact_vecs[fi])
            top = await repo.search(
                tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID, query_vector=qv, top_k=1,
                type_filter=None, tags_filter=None, include_shared=True,
                user_scope_visibility="isolated",
            )
            if top and top[0].id == f"{item['id']}-f{fi}":
                ext_top1 += 1
    n = total or 1
    return {
        "blob_sim": blob_sim_sum / n, "extracted_sim": ext_sim_sum / n,
        "extracted_top1_over_blob": ext_top1 / n, "total": total,
    }


def _ilad(mems: list[StoredMemory]) -> float:
    """Intra-List Average Distance (mean pairwise cosine DISTANCE) — higher = more diverse."""
    if len(mems) < 2:
        return 0.0
    dists: list[float] = []
    for i in range(len(mems)):
        for j in range(i + 1, len(mems)):
            dists.append(1.0 - _cos(mems[i].vector, mems[j].vector))
    return sum(dists) / len(dists) if dists else 0.0


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ── B6: MMR diversity (coverage + ILAD) on a redundancy-heavy fixture ─────────────────
async def run_mmr(data: dict) -> dict:
    """Compare pure-cosine vs MMR top_k on a redundancy-heavy multi-target fixture."""
    mems, queries = data["mmr_memories"], data["mmr_queries"]
    vocab = _vocab(mems, queries)
    now = datetime.now(UTC)

    async def _run(mmr: bool) -> tuple[float, float]:
        repo = InMemoryRepository()
        _seed(repo, mems, vocab, now=now)
        cov_scores: list[float] = []
        ilads: list[float] = []
        for q in queries:
            top = await repo.search(
                tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID,
                query_vector=_bow_vector(q["query"], vocab), top_k=2, type_filter=None,
                tags_filter=None, include_shared=True, user_scope_visibility="isolated",
                mmr_enabled=mmr, mmr_lambda=0.5,
            )
            got = {m.id for m in top}
            targets = set(q["targets"])
            cov_scores.append(len(got & targets) / len(targets) if targets else 0.0)
            ilads.append(_ilad(top))
        n = len(queries) or 1
        return sum(cov_scores) / n, sum(ilads) / n

    cos_cov, cos_ilad = await _run(False)
    mmr_cov, mmr_ilad = await _run(True)
    return {
        "cosine_coverage": cos_cov, "mmr_coverage": mmr_cov,
        "cosine_ilad": cos_ilad, "mmr_ilad": mmr_ilad,
    }


# ── B7: multi-hop link expansion recall ───────────────────────────────────────────────
async def run_linking(data: dict) -> dict:
    """A target reachable ONLY through a link: recall with vs without 1-hop expansion."""
    mems, queries = data["linked_memories"], data["link_queries"]
    vocab = _vocab(mems, queries)
    now = datetime.now(UTC)

    async def _recall(expand: bool) -> float:
        repo = InMemoryRepository()
        _seed(repo, mems, vocab, now=now)
        # Wire the golden edges directly (bidirectional, mirroring store()'s link writes).
        for src, dst in data["link_edges"]:
            repo._links.setdefault(src, []).append((dst, "associated", 1.0))  # noqa: SLF001
            repo._links.setdefault(dst, []).append((src, "associated", 1.0))  # noqa: SLF001
        hits = 0
        for q in queries:
            # top_k=1: cosine returns only the query-matching seed; the associated target is
            # reachable ONLY via the link, so it appears only when expansion is on.
            top = await repo.search(
                tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID,
                query_vector=_bow_vector(q["query"], vocab), top_k=1, type_filter=None,
                tags_filter=None, include_shared=True, user_scope_visibility="isolated",
                linking_enabled=expand, link_expansion_limit=10,
            )
            if q["expected"] in [m.id for m in top]:
                hits += 1
        return hits / len(queries) if queries else 0.0

    return {"vector_only_recall": await _recall(False), "expanded_recall": await _recall(True)}


def _read_golden(golden_path: Path) -> dict:
    return json.loads(golden_path.read_text(encoding="utf-8"))


async def run(golden_path: Path) -> tuple[Metrics, Metrics]:
    data = _read_golden(golden_path)
    memories, queries = data["memories"], data["queries"]
    vocab = _vocab(memories, queries)
    cosine = await _eval(await _load_repo(memories, vocab), queries, vocab, scoring_enabled=False)
    composite = await _eval(
        await _load_repo(memories, vocab), queries, vocab, scoring_enabled=True
    )
    return cosine, composite


def _print_table(cosine: Metrics, composite: Metrics) -> None:
    print(f"Golden queries: {cosine.total}   top_k={_TOP_K}\n")
    print(f"{'metric':<20}{'cosine':>12}{'composite':>12}{'delta':>10}")
    print("-" * 54)
    for name, c, k in (
        ("recall@k", cosine.recall_at_k, composite.recall_at_k),
        ("helpfulness", cosine.helpfulness, composite.helpfulness),
    ):
        print(f"{name:<20}{c:>12.3f}{k:>12.3f}{(k - c):>+10.3f}")
    print()
    verdict = "PASS" if composite.recall_at_k >= cosine.recall_at_k else "FAIL"
    print(f"composite recall@k >= cosine recall@k -> {verdict}")


async def run_extensions(golden_path: Path) -> dict:
    """Run the B1/B4/B5/B6/B7 eval extensions and return their metrics."""
    data = _read_golden(golden_path)
    return {
        "b1_halfvec_float16": float16_recall_delta(data),
        "b4_actr_frequency": await run_frequency_actr(data),
        "b5_extraction": await run_extraction(data),
        "b6_mmr_diversity": await run_mmr(data),
        "b7_link_expansion": await run_linking(data),
    }


def _print_extensions(ext: dict) -> None:
    b1 = ext["b1_halfvec_float16"]
    b4 = ext["b4_actr_frequency"]
    b5 = ext["b5_extraction"]
    b6 = ext["b6_mmr_diversity"]
    b7 = ext["b7_link_expansion"]
    print("\n-- Feature eval extensions -----------------------------------------")
    print(f"B1 halfvec: recall@1 f32={b1['recall_f32']:.3f} f16={b1['recall_f16']:.3f} "
          f"delta={b1['delta']:+.3f}  (near-zero => ~identical recall)")
    print(f"B4 ACT-R:   recall@1 cosine={b4['cosine_recall']:.3f} exp={b4['exponential_recall']:.3f} "
          f"power_actr={b4['actr_recall']:.3f}  (frequency tie-break)")
    print(f"B5 extract: query->fact sim blob={b5['blob_sim']:.3f} isolated={b5['extracted_sim']:.3f} "
          f"| focused fact top-1 over blob={b5['extracted_top1_over_blob']:.3f}")
    print(f"B6 MMR:     coverage cosine={b6['cosine_coverage']:.3f} mmr={b6['mmr_coverage']:.3f} "
          f"| ILAD cosine={b6['cosine_ilad']:.3f} mmr={b6['mmr_ilad']:.3f}")
    print(f"B7 linking: recall vector-only={b7['vector_only_recall']:.3f} "
          f"expanded={b7['expanded_recall']:.3f}  (multi-hop reachable only via a link)")


def main() -> int:
    golden = Path(__file__).resolve().parent / "golden_memories.json"
    cosine, composite = asyncio.run(run(golden))
    ext = asyncio.run(run_extensions(golden))
    if "--json" in sys.argv:
        print(json.dumps({
            "top_k": _TOP_K,
            "cosine": cosine.__dict__,
            "composite": composite.__dict__,
            "composite_no_regression": composite.recall_at_k >= cosine.recall_at_k,
            "extensions": ext,
        }, indent=2))
    else:
        _print_table(cosine, composite)
        _print_extensions(ext)
    return 0 if composite.recall_at_k >= cosine.recall_at_k else 1


if __name__ == "__main__":
    raise SystemExit(main())
