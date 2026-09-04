"""S5 Dense vector index: cosine-similarity retrieval over pre-computed embeddings.

Pure in-memory implementation.  No external dependencies beyond stdlib.
Used as one leg of hybrid retrieval (BM25 + dense + RRF).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from zhiwei.knowledge.indexes.fusion import ScoredDocument


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    denom = _norm(a) * _norm(b)
    if denom == 0.0:
        return 0.0
    return _dot(a, b) / denom


@dataclass
class _IndexedDocument:
    """Internal document storage with embedding."""

    doc_id: str
    content: str
    embedding: list[float]
    metadata: dict[str, object]


class DenseIndex:
    """In-memory dense vector index using brute-force cosine similarity.

    Stores pre-computed embeddings and scores queries against them.
    Suitable for small-to-medium corpora; production would use HNSW/IVF.
    """

    def __init__(self) -> None:
        self._documents: dict[str, _IndexedDocument] = {}
        self._dimension: int | None = None

    @property
    def size(self) -> int:
        """Number of indexed documents."""
        return len(self._documents)

    @property
    def dimension(self) -> int | None:
        """Embedding dimension, or None if empty."""
        return self._dimension

    def add(
        self,
        doc_id: str,
        content: str,
        embedding: list[float],
        **metadata: object,
    ) -> None:
        """Index a document with its pre-computed embedding vector.

        All embeddings must have the same dimension.
        """
        if self._dimension is not None and len(embedding) != self._dimension:
            raise ValueError(
                f"Embedding dimension mismatch: expected {self._dimension}, "
                f"got {len(embedding)}"
            )
        self._dimension = len(embedding)
        self._documents[doc_id] = _IndexedDocument(
            doc_id=doc_id,
            content=content,
            embedding=list(embedding),
            metadata=dict(metadata),
        )

    def remove(self, doc_id: str) -> None:
        """Remove a document from the index.  No-op if not found."""
        self._documents.pop(doc_id, None)

    def search(self, query_embedding: list[float], top_k: int = 10) -> list[ScoredDocument]:
        """Score all documents against the query embedding via cosine similarity.

        Returns the top_k most similar documents.
        """
        if not self._documents:
            return []

        if self._dimension is not None and len(query_embedding) != self._dimension:
            raise ValueError(
                f"Query embedding dimension mismatch: expected {self._dimension}, "
                f"got {len(query_embedding)}"
            )

        scores: list[ScoredDocument] = []
        for doc in self._documents.values():
            sim = _cosine_similarity(query_embedding, doc.embedding)
            scores.append(
                ScoredDocument(
                    doc_id=doc.doc_id,
                    score=sim,
                    content=doc.content,
                    metadata=doc.metadata,
                )
            )

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores[:top_k]

    def clear(self) -> None:
        """Remove all documents from the index."""
        self._documents.clear()
        self._dimension = None
