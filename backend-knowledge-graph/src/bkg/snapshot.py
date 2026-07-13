"""Bridge between the protocol (facts) and the store (memo rows), plus the
canonical whole-graph snapshot used by the determinism oracle.

- ``materialize`` turns a ``PartialGraph`` into ``MemoRow``s (one per node/edge),
  each carrying its canonical value bytes + BLAKE3 fingerprint.
- ``snapshot_bytes`` / ``snapshot_digest`` produce an order-independent canonical
  serialization of the store's current graph (sorted by key), so the same logical
  graph always yields the same digest regardless of insertion order or reload.

This is the only module that knows how a protocol fact becomes a stored row; in
P1 the incremental engine will produce the same rows through memoized queries.
"""

from __future__ import annotations

from collections.abc import Iterable

from .protocol.canonical import canonical_bytes, fingerprint, hexdigest
from .protocol.models import PartialGraph
from .store.base import GraphStore, MemoRow


def _row_for(fact: object) -> MemoRow:
    payload = fact.model_dump(mode="json")  # type: ignore[attr-defined]
    value = canonical_bytes(payload)
    return MemoRow(
        key=payload["id"],
        kind=str(payload["kind"]),
        value=value,
        value_fp=fingerprint(value),
        provenance=str(payload.get("provenance", "static")),
        confidence=str(payload.get("confidence", "static-certain")),
    )


def materialize(graph: PartialGraph) -> list[MemoRow]:
    """Turn every node and edge into a memo row. Node and edge id namespaces
    must not collide (they don't: node ids are ``route:``/``handler:``/... and
    edge ids are ``edge:``)."""
    rows = [_row_for(n) for n in graph.nodes]
    rows.extend(_row_for(e) for e in graph.edges)
    return rows


def load(store: GraphStore, rows: Iterable[MemoRow]) -> None:
    """Persist a batch of memo rows atomically."""
    with store.transaction():
        for row in rows:
            store.put_node(row)


def rows_snapshot_bytes(rows: Iterable[MemoRow]) -> bytes:
    """Canonical, order-independent serialization of an explicit set of memo rows.

    Used by the incremental engine to snapshot exactly the current-reachable set
    (rows verified at the current revision), and by ``snapshot_bytes`` for the
    whole store.
    """
    ordered = sorted(rows, key=lambda r: r.key)
    out = bytearray()
    for r in ordered:
        # Length-prefixed framing so a key/kind/value that happened to contain the
        # delimiter can never alias a different row-set to the same digest.
        for field in (r.key.encode("utf-8"), r.kind.encode("utf-8"), r.value):
            out += b"%d:" % len(field)
            out += field
    return bytes(out)


def rows_digest(rows: Iterable[MemoRow]) -> str:
    return hexdigest(rows_snapshot_bytes(rows))


def snapshot_bytes(store: GraphStore) -> bytes:
    """Canonical, order-independent serialization of the store's current graph."""
    return rows_snapshot_bytes(store.all_nodes())


def snapshot_digest(store: GraphStore) -> str:
    """BLAKE3 hex digest of the canonical snapshot — the oracle's ground truth."""
    return hexdigest(snapshot_bytes(store))
