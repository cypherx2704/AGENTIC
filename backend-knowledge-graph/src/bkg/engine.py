"""The demand-driven, memoized incremental engine (Layer A) — the moat proof.

A Salsa / rust-analyzer-style red-green engine. Every fact is a memoized query
node keyed by a stable nominal id, recording exactly which other nodes it read.
On an input change, nothing is eagerly recomputed: the next query lazily
re-verifies down the dependency graph, and a node is **recomputed only if some
dependency's VALUE actually changed** (early cutoff via fingerprint comparison).
If a recompute produces a byte-identical value, the node is **backdated** so the
cascade stops dead — a comment/whitespace edit costs nothing downstream.

Correctness properties (checked by the determinism oracle in the tests):
1. the incremental graph is byte-identical to a from-scratch rebuild;
2. every reachable node's dependency set equals a fresh build's (no stale/extra
   deps hiding behind a coincidentally-correct value);
3. genuinely incremental — a no-op edit advances zero downstream facts, and a
   redundant re-query recomputes nothing.

Design notes / invariants:
- **Inputs vs derived is structural:** a key is an INPUT iff its kind has no
  registered query. This needs no in-memory state, so it survives a reload of an
  existing store (no volatile ``_inputs`` map to rehydrate).
- **Values are immutable-per-read:** every read returns a fresh ``json.loads`` of
  the stored canonical bytes, so a query that mutates what it reads cannot
  corrupt the graph.
- The engine is storage-agnostic (only the ``GraphStore`` port) and framework-
  agnostic (queries are registered from outside).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .protocol.canonical import canonical_bytes, fingerprint
from .snapshot import rows_digest
from .store.base import Dep, GraphStore, MemoRow

QueryFn = Callable[[str, "Cx"], Any]


def _kind(key: str) -> str:
    """A key is ``<kind>:<arg>``; the kind selects the query function."""
    return key.split(":", 1)[0]


class Cx:
    """The context a query runs in. ``read`` is the ONLY way to depend on another
    node — it forces the dependency and records the edge plus the dependency's
    value fingerprint at read time (for early cutoff). Reads are de-duplicated by
    key so a query may read the same dependency more than once safely."""

    def __init__(self, engine: Engine, rev: int, owner: str) -> None:
        self._engine = engine
        self._rev = rev
        self._owner = owner
        self._deps: dict[str, Dep] = {}

    def read(self, dep_key: str) -> Any:
        self._engine._force(dep_key, self._rev)
        node = self._engine._store.get_node(dep_key)
        if node is None:
            raise KeyError(f"read of missing node {dep_key!r}")
        if dep_key not in self._deps:  # keep first read's fingerprint (they agree)
            self._deps[dep_key] = Dep(
                node_key=self._owner, dep_key=dep_key, dep_fp_at_read=node.value_fp
            )
        return self._engine._value_of(node)

    @property
    def deps(self) -> list[Dep]:
        return list(self._deps.values())


class Engine:
    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._queries: dict[str, QueryFn] = {}
        self._forcing: list[str] = []  # active recompute stack (cycle detection)
        # instrumentation (the oracle asserts on these)
        self.recompute_count = 0
        self.recomputed: set[str] = set()

    # ------------------------------------------------------------------ setup
    def define_query(self, kind: str, fn: QueryFn) -> None:
        self._queries[kind] = fn

    def reset_counters(self) -> None:
        self.recompute_count = 0
        self.recomputed = set()

    def _is_input(self, key: str) -> bool:
        """An input is any key whose kind has no registered query. Structural, so
        it is durable across a store reload (no in-memory input map to rebuild)."""
        return _kind(key) not in self._queries

    # ------------------------------------------------------------------ inputs
    def set_input(self, key: str, value: Any) -> None:
        """Set (or replace) an input leaf. Bumps the global revision; only marks
        this input as *changed* if its canonical value actually differs."""
        vb = canonical_bytes(value)  # snapshots the value to bytes immediately
        fp = fingerprint(vb)
        with self._store.transaction():
            rev = self._store.bump_revision()
            old = self._store.get_node(key)
            changed = rev if (old is None or old.value_fp != fp) else old.changed_rev
            self._store.put_node(
                MemoRow(
                    key=key,
                    kind=_kind(key),
                    value=vb,
                    value_fp=fp,
                    changed_rev=changed,
                    verified_rev=rev,
                    provenance="static",
                    confidence="static-certain",
                )
            )

    def remove_input(self, key: str) -> None:
        """Remove an input and invalidate everything that (transitively) read it.

        Deletes the reverse-dependency closure's memo rows + dep edges, so no
        stale edge points into the removed subtree and the next query rebuilds
        the affected nodes cleanly from the current inputs. (Coarse but correct:
        because ``all_mounts`` reads every file, removing one file invalidates the
        mount-derived nodes broadly; surgical file-delete invalidation is a future
        optimization. Callers should drop the key from any manifest input first.)"""
        with self._store.transaction():
            self._store.bump_revision()
            for node_key in self._rdep_closure(key):
                self._store.put_deps(node_key, [])
                self._store.delete_node(node_key)

    def _rdep_closure(self, key: str) -> set[str]:
        seen: set[str] = set()
        stack = [key]
        while stack:
            node_key = stack.pop()
            if node_key in seen:
                continue
            seen.add(node_key)
            stack.extend(self._store.get_rdeps(node_key))
        return seen

    def reverse_dependencies(self, key: str) -> set[str]:
        """The transitive reverse-dependency closure of a node (including the node
        itself) — i.e. its blast radius. Requires the graph to be built first."""
        return self._rdep_closure(key)

    # ------------------------------------------------------------ query / force
    def query(self, key: str) -> Any:
        """Bring ``key`` up to date at the current revision and return its value."""
        with self._store.transaction():
            rev = self._store.get_revision()
            self._force(key, rev)
            node = self._store.get_node(key)
            assert node is not None
            return self._value_of(node)

    def _force(self, key: str, rev: int) -> None:
        """Ensure ``key`` has a value valid at revision ``rev``."""
        node = self._store.get_node(key)
        if node is not None and node.verified_rev == rev:
            return  # already checked this revision

        if self._is_input(key):
            # Inputs are set externally; their value is current by construction.
            if node is None:
                raise KeyError(f"missing input {key!r}")
            self._store.put_node(replace(node, verified_rev=rev))
            return

        if node is not None:
            deps = self._store.get_deps(key)
            # A node can be GREEN only if it has recorded dependencies AND every
            # one is byte-identical to what it read last time. A node with zero
            # recorded deps is never vacuously green — it is always recomputed.
            if deps:
                green = True
                for dep in deps:
                    if self._store.get_node(dep.dep_key) is None:
                        green = False  # a dependency disappeared -> recompute
                        break
                    self._force(dep.dep_key, rev)
                    cur = self._store.get_node(dep.dep_key)
                    if cur is None or cur.value_fp != dep.dep_fp_at_read:
                        green = False
                        break
                if green:
                    self._store.put_node(replace(node, verified_rev=rev))
                    return

        self._recompute(key, rev)

    def _recompute(self, key: str, rev: int) -> None:
        if key in self._forcing:
            raise RuntimeError("cycle detected: " + " -> ".join([*self._forcing, key]))
        self._forcing.append(key)
        try:
            cx = Cx(self, rev, key)
            value = self._run(key, cx)
        finally:
            self._forcing.pop()

        vb = canonical_bytes(value)
        fp = fingerprint(vb)
        old = self._store.get_node(key)
        # EARLY CUTOFF: identical value -> keep changed_rev (backdate), so nodes
        # downstream stay green; otherwise the value truly changed at this revision.
        changed = old.changed_rev if (old is not None and old.value_fp == fp) else rev
        self._store.put_node(
            MemoRow(
                key=key,
                kind=_kind(key),
                value=vb,
                value_fp=fp,
                changed_rev=changed,
                verified_rev=rev,
                provenance="static",
                confidence="inferred",
            )
        )
        self._store.put_deps(key, cx.deps)
        self.recompute_count += 1
        self.recomputed.add(key)

    def _run(self, key: str, cx: Cx) -> Any:
        fn = self._queries.get(_kind(key))
        if fn is None:
            raise KeyError(f"no query registered for kind {_kind(key)!r} (key {key!r})")
        return fn(key, cx)

    def _value_of(self, node: MemoRow) -> Any:
        # Always a fresh decode: values are immutable-per-read, so a query that
        # mutates what it reads cannot corrupt the stored graph.
        return json.loads(node.value)

    # --------------------------------------------------------------- snapshot
    def snapshot_rows(self, root: str) -> list[MemoRow]:
        """Force ``root``, then return the nodes REACHABLE from it via the current
        dependency edges. Reachability — not ``verified_rev == rev`` — is the
        correct liveness test: try-mark-green forces a parent's *previous* deps to
        check them, which re-verifies nodes the parent then drops when it
        recomputes with a smaller dep set (e.g. a deleted route's facts)."""
        with self._store.transaction():
            rev = self._store.get_revision()
            self._force(root, rev)
            reachable: dict[str, MemoRow] = {}
            stack = [root]
            while stack:
                key = stack.pop()
                if key in reachable:
                    continue
                node = self._store.get_node(key)
                if node is None:  # pragma: no cover - defensive
                    raise KeyError(f"reachable node missing: {key!r}")
                reachable[key] = node
                stack.extend(dep.dep_key for dep in self._store.get_deps(key))
            return list(reachable.values())

    def snapshot_digest(self, root: str) -> str:
        return rows_digest(self.snapshot_rows(root))

    def dep_map(self, root: str) -> dict[str, list[str]]:
        """{reachable key -> sorted dependency keys}. The oracle asserts this
        equals a fresh rebuild's — catching stale/extra/missing dep edges whose
        current value happens to be correct (which the value digest cannot)."""
        rows = self.snapshot_rows(root)
        return {r.key: sorted(d.dep_key for d in self._store.get_deps(r.key)) for r in rows}
