"""S5 Reranker: post-retrieval reranking stage.

Provides the reranker port and a pass-through implementation.
Production rerankers (cross-encoder, CPU BGE) implement the same interface.
"""

from __future__ import annotations

from zhiwei.knowledge.indexes.fusion import ScoredDocument


class Reranker:
    """Base reranker port.

    Subclasses override rerank() with a cross-encoder or learned model.
    The default implementation preserves input order (pass-through).
    """

    def rerank(
        self,
        query: str,
        candidates: list[ScoredDocument],
        *,
        top_k: int = 10,
    ) -> list[ScoredDocument]:
        """Rerank candidates by relevance to the query.

        Args:
            query: The original query string.
            candidates: Pre-ranked documents from fusion.
            top_k: Maximum results to return.

        Returns:
            Reranked documents limited to top_k.
        """
        return candidates[:top_k]
