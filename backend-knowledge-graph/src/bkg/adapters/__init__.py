"""Framework adapters — deterministic, file-local fact extractors.

An adapter turns ONE file's source into file-local facts (routes, mounts,
imports). It never resolves cross-file references or assembles endpoints — that
is the pipeline's job at query time. Python targets use the stdlib ``ast``;
other languages will use tree-sitter/sidecars.
"""
