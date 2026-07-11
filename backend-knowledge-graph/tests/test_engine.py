"""Unit tests for the incremental engine mechanics: early cutoff, backdating,
no-op no-recompute, cycle detection, and the properties the adversarial review
locked in (duplicate reads, zero-dep nodes, mutation safety)."""

from __future__ import annotations

import pytest

from bkg.engine import Cx, Engine
from bkg.store import open_store


def _parity_engine() -> Engine:
    """in:0 (int) -> proj:0 (in % 2) -> cons:0 (proj * 10)."""
    engine = Engine(open_store(":memory:"))

    def proj(key: str, cx: Cx) -> int:
        return int(cx.read("in:0")) % 2

    def cons(key: str, cx: Cx) -> int:
        return int(cx.read("proj:0")) * 10

    engine.define_query("proj", proj)
    engine.define_query("cons", cons)
    return engine


def test_input_change_recomputes_dependent() -> None:
    engine = _parity_engine()
    engine.set_input("in:0", 2)
    assert engine.query("cons:0") == 0

    engine.set_input("in:0", 3)  # parity flips 0 -> 1
    engine.reset_counters()
    assert engine.query("cons:0") == 10
    assert "proj:0" in engine.recomputed
    assert "cons:0" in engine.recomputed


def test_backdating_stops_cascade() -> None:
    engine = _parity_engine()
    engine.set_input("in:0", 2)
    engine.query("cons:0")

    engine.set_input("in:0", 4)  # still even -> parity unchanged
    engine.reset_counters()
    assert engine.query("cons:0") == 0
    assert "proj:0" in engine.recomputed  # projection re-ran (its input changed)...
    assert "cons:0" not in engine.recomputed  # ...but the cascade stopped (backdated)


def test_no_op_force_recomputes_nothing() -> None:
    engine = _parity_engine()
    engine.set_input("in:0", 2)
    engine.query("cons:0")

    engine.reset_counters()
    engine.query("cons:0")  # same revision, nothing changed
    assert engine.recompute_count == 0


def test_cycle_detection() -> None:
    engine = Engine(open_store(":memory:"))
    engine.define_query("x", lambda k, cx: cx.read("y:0"))
    engine.define_query("y", lambda k, cx: cx.read("x:0"))
    with pytest.raises(RuntimeError, match="cycle"):
        engine.query("x:0")


def test_duplicate_read_of_same_dep_is_safe() -> None:
    """H3: reading the same dependency twice must not violate the deps PK."""
    engine = Engine(open_store(":memory:"))

    def sum2(key: str, cx: Cx) -> int:
        return int(cx.read("in:0")) + int(cx.read("in:0"))  # same dep, twice

    engine.define_query("sum2", sum2)
    engine.set_input("in:0", 5)
    assert engine.query("sum2:0") == 10
    engine.set_input("in:0", 7)
    assert engine.query("sum2:0") == 14


def test_zero_dep_query_is_never_vacuously_green() -> None:
    """H2: a node that records zero deps must be recomputed, never assumed green
    (otherwise a query that read via a side channel would go stale forever)."""
    engine = Engine(open_store(":memory:"))
    calls = {"n": 0}

    def const(key: str, cx: Cx) -> int:
        calls["n"] += 1
        return 42

    engine.define_query("const", const)
    engine.set_input("trigger:0", 0)
    assert engine.query("const:0") == 42
    before = calls["n"]

    engine.set_input("trigger:0", 1)  # new revision; const has no recorded deps
    engine.query("const:0")
    assert calls["n"] > before  # it re-ran rather than serving a vacuous-green cache


def test_mutating_query_cannot_corrupt_the_graph() -> None:
    """H1: values are immutable-per-read, so a query that mutates what it reads
    still matches a fresh rebuild across an edit."""
    engine = Engine(open_store(":memory:"))

    def badlen(key: str, cx: Cx) -> int:
        data = cx.read("in:0")  # a list
        data.append("X")  # BUGGY: mutates the read value
        return len(data)

    engine.define_query("badlen", badlen)
    engine.set_input("in:0", [1, 2])
    assert engine.query("badlen:0") == 3

    engine.set_input("in:0", [1, 2, 3])  # forces recompute
    assert engine.query("badlen:0") == 4  # fresh copy each read -> no accumulation
