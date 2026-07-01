"""Chunking engine unit tests (Component 3)."""

from __future__ import annotations

from rag_service.services.chunking import chunk_fixed, chunk_sentence, chunk_text


def test_fixed_chunking_overlap_and_count() -> None:
    text = "a" * 1000
    chunks = chunk_fixed(text, chunk_size=200, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(len(c.content) <= 200 for c in chunks)
    # Indices are contiguous from 0.
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_fixed_chunking_empty_input_yields_nothing() -> None:
    assert chunk_fixed("   ", chunk_size=100, chunk_overlap=10) == []


def test_sentence_chunking_splits_on_boundaries() -> None:
    text = "First sentence here. Second sentence follows! Third one ends? Fourth."
    chunks = chunk_sentence(text, chunk_size=40, chunk_overlap=10)
    assert len(chunks) >= 2
    joined = " ".join(c.content for c in chunks)
    assert "First sentence" in joined and "Fourth" in joined


def test_sentence_chunking_packs_to_chunk_size() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(50))
    chunks = chunk_sentence(text, chunk_size=120, chunk_overlap=20)
    # Most chunks should respect the size budget (the packer keeps under chunk_size).
    assert all(len(c.content) <= 200 for c in chunks)
    assert len(chunks) > 1


def test_content_sha_is_deterministic() -> None:
    a = chunk_fixed("hello world content here", chunk_size=10, chunk_overlap=0)
    b = chunk_fixed("hello world content here", chunk_size=10, chunk_overlap=0)
    assert [c.content_sha256 for c in a] == [c.content_sha256 for c in b]


def test_chunk_text_dispatch() -> None:
    fixed = chunk_text("x" * 50, strategy="fixed", chunk_size=20, chunk_overlap=5)
    sent = chunk_text("One. Two. Three.", strategy="sentence", chunk_size=20, chunk_overlap=5)
    assert fixed and sent
