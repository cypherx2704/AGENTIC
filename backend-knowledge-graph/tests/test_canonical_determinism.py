"""P0 determinism harness — the foundation the whole moat stands on.

The same logical graph must produce a byte-identical canonical snapshot digest
regardless of insertion order, repetition, or store teardown/reload, and must be
OS-independent by construction (no backslashes / drive letters / absolute paths).
"""

from __future__ import annotations

import random
import re
from dataclasses import replace

from bkg.protocol.models import PartialGraph
from bkg.snapshot import load, materialize, snapshot_bytes, snapshot_digest
from bkg.store import open_store


def _digest(rows: list) -> str:
    store = open_store(":memory:")
    load(store, rows)
    digest = snapshot_digest(store)
    store.close()
    return digest


def test_digest_stable_across_repeats(sample_graph: PartialGraph) -> None:
    rows = materialize(sample_graph)
    assert len({_digest(list(rows)) for _ in range(100)}) == 1


def test_digest_stable_across_shuffled_insertion(sample_graph: PartialGraph) -> None:
    rows = materialize(sample_graph)
    base = _digest(list(rows))
    for seed in range(25):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert _digest(shuffled) == base


def test_digest_stable_across_reload(tmp_path, sample_graph: PartialGraph) -> None:
    db = str(tmp_path / "graph.db")
    rows = materialize(sample_graph)

    store = open_store(db)
    load(store, rows)
    before = snapshot_digest(store)
    store.close()

    reopened = open_store(db)
    after = snapshot_digest(reopened)
    reopened.close()

    assert before == after


def test_snapshot_is_os_independent(sample_graph: PartialGraph) -> None:
    store = open_store(":memory:")
    load(store, materialize(sample_graph))
    data = snapshot_bytes(store)
    store.close()

    assert b"\\" not in data  # no Windows path separators leaked in
    # no drive-letter absolute paths (\b so a real "C:/" matches but "POST:/" does not)
    assert re.search(rb"\b[A-Za-z]:[\\/]", data) is None


def test_fingerprint_isolates_change(sample_graph: PartialGraph) -> None:
    """Changing one fact must change ONLY that fact's fingerprint — the property
    early cutoff relies on to stop cascades at the changed node."""
    before = {r.key: r.value_fp for r in materialize(sample_graph)}

    nodes = list(sample_graph.nodes)
    idx = next(i for i, n in enumerate(nodes) if n.id.startswith("route:"))
    changed = nodes[idx].model_copy(update={"line": 999})
    nodes[idx] = changed
    mutated = replace(sample_graph, nodes=tuple(nodes))

    after = {r.key: r.value_fp for r in materialize(mutated)}

    assert after[changed.id] != before[changed.id]
    assert all(after[k] == before[k] for k in before if k != changed.id)
