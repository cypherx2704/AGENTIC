"""Pluggable vector storage (Component 5e). IVectorStore + PgVectorAdapter."""

from .base import ChunkHit, ChunkVector, IVectorStore, StorageStats
from .pgvector import PgVectorAdapter, resolve_vector_store

__all__ = [
    "ChunkHit",
    "ChunkVector",
    "IVectorStore",
    "PgVectorAdapter",
    "StorageStats",
    "resolve_vector_store",
]
