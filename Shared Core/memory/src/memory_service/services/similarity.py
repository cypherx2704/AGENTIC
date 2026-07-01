"""Cosine similarity for in-process vector ranking (the fake repo + dedup math).

The Postgres repo ranks with pgvector's HNSW ``<=>`` cosine-distance operator; this
module is the Python equivalent used by the in-memory repository (tests) and by any
caller that needs to compare two already-fetched vectors. Vectors from the embedder are
L2-normalized, so cosine similarity == dot product, but we normalize defensively here so
the function is correct for any input.
"""

from __future__ import annotations

import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, in [-1, 1] (0 on a zero vector)."""
    if len(a) != len(b):
        raise ValueError("vectors must be the same length")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom
