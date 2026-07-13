"""The DB-agnostic ``GraphStore`` port + its row value objects.

Nothing in bkg outside a concrete implementation imports a database driver.
Swapping SQLite for another backend later = writing one new subclass; no other
module changes. The schema this port implies (memo / deps / inputs / meta) is
frozen here even though the incremental engine that populates deps/inputs lands
in P1 — freezing it now keeps later work additive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoRow:
    """One memoized fact: a node, an edge, or (later) a derived query result."""

    key: str
    kind: str
    value: bytes  # canonical-serialized fact
    value_fp: bytes  # BLAKE3(value) — the early-cutoff fingerprint
    changed_rev: int = 0  # revision the VALUE last actually changed (backdating)
    verified_rev: int = 0  # revision last confirmed green
    provenance: str = "static"
    confidence: str = "static-certain"
    partial: bool = False


@dataclass(frozen=True)
class Dep:
    node_key: str
    dep_key: str
    dep_fp_at_read: bytes


@dataclass(frozen=True)
class InputRow:
    input_id: str
    content_fp: bytes
    changed_rev: int


class GraphStore(ABC):
    """Persistence port for the memoized graph. Implementations are the only
    modules aware of the concrete database."""

    # --- global revision ---------------------------------------------------
    @abstractmethod
    def get_revision(self) -> int: ...

    @abstractmethod
    def bump_revision(self) -> int: ...

    # --- memo (nodes / edges / derived facts) ------------------------------
    @abstractmethod
    def get_node(self, key: str) -> MemoRow | None: ...

    @abstractmethod
    def put_node(self, row: MemoRow) -> None: ...

    @abstractmethod
    def delete_node(self, key: str) -> None: ...

    @abstractmethod
    def all_nodes(self) -> Iterable[MemoRow]:
        """Every memo row. Ordering is NOT guaranteed — callers sort by key."""

    # --- dependency graph (used by the P1 engine) --------------------------
    @abstractmethod
    def get_deps(self, key: str) -> list[Dep]: ...

    @abstractmethod
    def put_deps(self, key: str, deps: list[Dep]) -> None: ...

    @abstractmethod
    def get_rdeps(self, dep_key: str) -> list[str]:
        """Reverse deps: nodes that read ``dep_key``. Cheap invalidation +
        blast-radius reverse index in one."""

    # --- inputs (used by the P1 engine) ------------------------------------
    @abstractmethod
    def get_input(self, input_id: str) -> InputRow | None: ...

    @abstractmethod
    def set_input(self, input_id: str, content_fp: bytes, changed_rev: int) -> None: ...

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Atomic unit of work; commits on success, rolls back on exception."""

    @abstractmethod
    def close(self) -> None: ...
