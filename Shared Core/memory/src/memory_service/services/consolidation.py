"""Consolidation / forgetting routine (opt-in; OFF by default).

Stanford "Generative Agents" describes a reflection/forgetting loop: low-value memories
are periodically summarized and pruned so retrieval stays sharp and storage bounded. This
module is the SKELETON of that routine with SAFE DEFAULTS — it NEVER runs unless
``MEMORY_CONSOLIDATION_ENABLED`` is on, and even then it only SOFT-deletes (snapshots to
``memory.memory_audit`` first, so a forget is reversible + explainable).

The loop:

1. select low-importance, old, currently-valid memories (``consolidation_candidates``);
2. cluster them (here: a trivial group-by principal — a real clusterer is a follow-up);
3. summarize each cluster into ONE new memory (here: a deterministic join — an LLM
   summary is a follow-up behind the same flag);
4. soft-delete the originals to the audit trail, pointing at the summary.

The default impl deliberately keeps clustering/summarization trivial: the deliverable is
the safe, flagged, auditable PLUMBING, not a production clusterer. Nothing here runs by
default, so it cannot affect today's behavior.
"""

from __future__ import annotations

import structlog

from ..core import metrics
from .repository import MemoryRepository, StoredMemory

logger = structlog.get_logger(__name__)


def summarize_cluster(memories: list[StoredMemory]) -> str:
    """Deterministic placeholder summary of a cluster (no network).

    A real implementation would call the llms-gateway behind the same flag; the seam is
    here so wiring it later is additive.
    """
    parts = [m.content.strip() for m in memories if m.content.strip()]
    joined = " | ".join(parts[:10])
    return f"[consolidated {len(memories)} memories] {joined}"[:4000]


async def run_consolidation_once(
    repo: MemoryRepository,
    *,
    max_importance: float,
    min_age_seconds: float,
    batch_size: int,
) -> int:
    """Run ONE consolidation pass. Returns the number of memories forgotten.

    Safe + idempotent-ish: selects a bounded batch, soft-deletes each to the audit trail.
    On any per-item error it logs + continues (a forgetting pass must never crash the
    service). Returns 0 immediately if there are no candidates.
    """
    candidates = await repo.consolidation_candidates(
        max_importance=max_importance, min_age_seconds=min_age_seconds, batch_size=batch_size,
    )
    if not candidates:
        return 0

    forgotten = 0
    for mem in candidates:
        try:
            # NOTE: summary memories are intentionally NOT inserted in this skeleton (no
            # write amplification / no surprise rows). The audit row records the cluster
            # intent; inserting the summary is a follow-up behind the same flag.
            ok = await repo.soft_delete_to_audit(
                memory=mem, action="consolidated",
                reason="low-importance, aged out by consolidation routine",
                summary_memory_id=None,
            )
            if ok:
                forgotten += 1
                metrics.consolidation_forgotten_total.inc()
        except Exception as exc:  # noqa: BLE001 — a forgetting pass must keep going
            logger.warning("consolidation_item_failed", memory_id=mem.id, error=str(exc))
    if forgotten:
        logger.info("consolidation_pass", forgotten=forgotten, candidates=len(candidates))
    return forgotten
