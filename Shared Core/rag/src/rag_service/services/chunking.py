"""Chunking engine (Component 3) — first-cycle ``fixed`` + ``sentence`` strategies.

``fixed``     — split every ``chunk_size`` characters with ``chunk_overlap`` overlap.
``sentence``  — split on sentence boundaries (regex), then greedily pack sentences into
                chunks up to ``chunk_size`` characters, carrying ``chunk_overlap``
                characters of tail context into the next chunk.

Both produce a list of ``Chunk`` (content + index). Empty / whitespace-only input yields
no chunks. Deterministic — the same text + config always yields the same chunks (so the
worker-side ``(doc_id, content_sha256)`` dedup is stable across retries).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Sentence boundary: end punctuation followed by whitespace. Simple + dependency-free
# (the spaCy upgrade is a 📋 enterprise item).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class Chunk:
    content: str
    chunk_index: int

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    """Normalise whitespace; keep the text otherwise intact."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def chunk_fixed(text: str, *, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    cleaned = _clean(text)
    if not cleaned:
        return []
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size - 1)
    step = chunk_size - chunk_overlap
    chunks: list[Chunk] = []
    for idx, start in enumerate(range(0, len(cleaned), step)):
        piece = cleaned[start : start + chunk_size]
        if not piece:
            break
        chunks.append(Chunk(content=piece, chunk_index=idx))
        if start + chunk_size >= len(cleaned):
            break
    return chunks


def chunk_sentence(text: str, *, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    cleaned = _clean(text)
    if not cleaned:
        return []
    sentences = [s for s in _SENTENCE_RE.split(cleaned) if s]
    if not sentences:
        return []

    chunks: list[Chunk] = []
    idx = 0
    buf = ""
    for sentence in sentences:
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if len(candidate) <= chunk_size or not buf:
            buf = candidate
        else:
            chunks.append(Chunk(content=buf, chunk_index=idx))
            idx += 1
            # Carry overlap chars of tail context into the next chunk.
            tail = buf[-chunk_overlap:] if chunk_overlap > 0 else ""
            buf = f"{tail} {sentence}".strip() if tail else sentence
        # A single oversized sentence: hard-split it via the fixed strategy.
        if len(buf) > chunk_size:
            for sub in chunk_fixed(buf, chunk_size=chunk_size, chunk_overlap=chunk_overlap)[:-1]:
                chunks.append(Chunk(content=sub.content, chunk_index=idx))
                idx += 1
            buf = chunk_fixed(buf, chunk_size=chunk_size, chunk_overlap=chunk_overlap)[-1].content
    if buf:
        chunks.append(Chunk(content=buf, chunk_index=idx))
    return chunks


def chunk_text(
    text: str, *, strategy: str, chunk_size: int, chunk_overlap: int
) -> list[Chunk]:
    """Dispatch to the configured chunking strategy."""
    if strategy == "fixed":
        return chunk_fixed(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    # Default + 'sentence'.
    return chunk_sentence(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
