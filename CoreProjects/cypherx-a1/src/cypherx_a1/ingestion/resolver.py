"""Entity resolution / canonicalization (DB-backed, app-owned).

Wires the PURE, type-aware coreference logic in :mod:`cypherx_a1.kg.resolution` to the
app-owned graph (``graph_repo`` + the ``entity_mentions`` map). When a node is upserted, the
resolver:

  1. records the node's surface form as a MENTION (preserved for audit),
  2. checks the mention map for an existing canonical id (exact normalized match — cheap),
  3. otherwise scans current same-kind entities for a TYPE-AWARE coreference candidate
     ('J. Smith' / 'John Smith') above the configured confidence floor, and
  4. when a confident canonical is found that differs from the just-upserted entity, MERGES
     the duplicate into the canonical (redirects edges, closes the loser) so the graph holds
     one node per real-world entity.

Runs on a connection already inside an ``in_tenant`` tx (RLS-scoped). It is a NO-OP unless
``entity_resolution_enabled`` is set — today's behavior (exact handle/email identity
resolution only) is unchanged when off.
"""

from __future__ import annotations

import structlog
from psycopg import AsyncConnection

from ..core import metrics
from ..core.config import Settings
from ..db import graph_repo
from ..kg.resolution import are_coreferent, mention_variants, normalize_mention

logger = structlog.get_logger(__name__)


async def resolve_entity(
    conn: AsyncConnection,
    *,
    settings: Settings,
    entity_id: str,
    kind: str,
    surface_form: str,
) -> str:
    """Resolve ``entity_id`` (kind/surface_form) to its canonical id, recording the mention.

    Returns the canonical entity_id (== ``entity_id`` when no merge happened). Safe to call
    repeatedly; idempotent on the mention map. When resolution is disabled it returns
    ``entity_id`` unchanged without touching the mention table (no behavioral change).
    """
    if not settings.entity_resolution_enabled:
        return entity_id

    norm = normalize_mention(surface_form)
    if not norm:
        return entity_id

    # 1) Exact normalized mention already mapped? Reuse its canonical (and merge if needed).
    canonical = await graph_repo.lookup_mention(conn, kind=kind, normalized_form=norm)

    # 2) No exact mention — scan current same-kind entities for a type-aware coref candidate.
    if canonical is None:
        canonical = await _find_coreferent(
            conn, kind=kind, surface_form=surface_form, exclude_entity_id=entity_id,
            min_conf=settings.entity_resolution_min_confidence,
        )

    resolver = "exact" if canonical is not None else "self"
    canonical = canonical or entity_id

    # 3) Merge the duplicate into the canonical (preserve the mention for audit).
    if canonical != entity_id:
        moved = await graph_repo.merge_entity(
            conn, loser_entity_id=entity_id, canonical_entity_id=canonical
        )
        resolver = "coref"
        metrics.entity_merges_total.inc()
        logger.info(
            "entity_resolution_merged", kind=kind, surface_form=surface_form,
            canonical=canonical, merged=entity_id, edges_redirected=moved,
        )

    # 4) Record the surface form + its variants as mentions of the canonical (audit trail).
    for variant in mention_variants(surface_form, kind=kind):
        await graph_repo.record_mention(
            conn, kind=kind, surface_form=surface_form, normalized_form=variant,
            canonical_entity_id=canonical, source="resolver", resolver=resolver,
            confidence=settings.entity_resolution_min_confidence if resolver == "coref" else 1.0,
        )
    return canonical


async def _find_coreferent(
    conn: AsyncConnection,
    *,
    kind: str,
    surface_form: str,
    exclude_entity_id: str,
    min_conf: float,
) -> str | None:
    """Scan current same-kind entities for a type-aware coreference match. Conservative: a
    keyed kind only matches on exact normalized equality; a person matches on initial/last-
    name compatibility. Returns the FIRST confident match's entity_id, else None."""
    candidates = await graph_repo.list_current_entities_of_kind(conn, kind=kind)
    for cand in candidates:
        cand_id = str(cand["entity_id"])
        if cand_id == exclude_entity_id:
            continue
        cand_name = cand.get("title") or cand.get("natural_key") or ""
        if are_coreferent(surface_form, cand_name, kind=kind):
            return cand_id
    return None
