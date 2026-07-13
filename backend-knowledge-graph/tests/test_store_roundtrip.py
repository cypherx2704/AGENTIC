"""Store round-trip + schema exercise (memo / revision / deps+rdeps).

The deps/inputs tables aren't populated until the P1 engine, but their schema is
frozen now, so these tests keep it honest and additive.
"""

from __future__ import annotations

from bkg.protocol.models import PartialGraph
from bkg.snapshot import load, materialize
from bkg.store import open_store
from bkg.store.base import Dep


def test_put_get_roundtrip(sample_graph: PartialGraph) -> None:
    store = open_store(":memory:")
    rows = materialize(sample_graph)
    load(store, rows)

    for row in rows:
        got = store.get_node(row.key)
        assert got is not None
        assert got.value == row.value
        assert got.value_fp == row.value_fp
        assert got.kind == row.kind

    assert len(list(store.all_nodes())) == len(rows)
    store.close()


def test_revision_persists(tmp_path) -> None:
    db = str(tmp_path / "g.db")

    store = open_store(db)
    assert store.get_revision() == 0
    assert store.bump_revision() == 1
    store.close()

    reopened = open_store(db)
    assert reopened.get_revision() == 1
    reopened.close()


def test_deps_and_rdeps() -> None:
    store = open_store(":memory:")
    with store.transaction():
        store.put_deps("a", [Dep("a", "b", b"x"), Dep("a", "c", b"y")])

    assert {d.dep_key for d in store.get_deps("a")} == {"b", "c"}
    assert store.get_rdeps("b") == ["a"]
    assert store.get_rdeps("nonexistent") == []
    store.close()
