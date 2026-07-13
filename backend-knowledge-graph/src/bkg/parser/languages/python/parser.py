"""Python ``LanguageParser`` backed by tree-sitter (grammar: ``tree-sitter-python``).

Wraps the pinned ``tree_sitter_python`` grammar. The grammar version is part of the
determinism contract: a grammar bump can change the tree and therefore the graph digest,
so it is pinned in ``uv.lock`` and treated as a breaking change. Parsing is pure and
never raises — tree-sitter error-recovers into a tree with ERROR nodes, which the
resolver maps to a ``partial`` model (an unparseable file contributes no facts).
"""

from __future__ import annotations

import tree_sitter_python as tsp
from tree_sitter import Language, Parser, Tree

_LANGUAGE = Language(tsp.language())


class TreeSitterPythonParser:
    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def parse(self, source: str) -> Tree:
        if source.startswith("﻿"):  # strip a leading UTF-8 BOM (common on Windows files)
            source = source[1:]
        return self._parser.parse(bytes(source, "utf-8"))
