"""S5 OpenSearch index port: adapter between domain indexes and external search.

This is the port layer that sits between the domain retrieval logic and
the external OpenSearch cluster.  It delegates to the concrete BM25/dense
implementations and provides alias-switch and rebuild capabilities.

Kill/rebuild: create a new index version, bulk-load, switch alias atomically.
The Source Ledger is never modified during index operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zhiwei.knowledge.indexes.dense import DenseIndex
from zhiwei.knowledge.indexes.fusion import ScoredDocument, fuse
from zhiwei.knowledge.indexes.lexical import BM25Index
from zhiwei.knowledge.indexes.rerank import Reranker


@dataclass(frozen=True)
class IndexStats:
    """Statistics for an index version."""

    version: str
    lexical_count: int
    dense_count: int
    alias: str


class OpenSearchPort:
    """Port for OpenSearch index operations.

    Provides:
    - Hybrid retrieval (BM25 + dense + RRF + rerank)
    - Index version management with alias switching
    - Kill/rebuild without touching Source Ledger
    """

    def __init__(
        self,
        *,
        alias: str = "knowledge",
        lexical: BM25Index | None = None,
        dense: DenseIndex | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._alias = alias
        self._lexical = lexical or BM25Index()
        self._dense = dense or DenseIndex()
        self._reranker = reranker or Reranker()
        self._version: str = "v1"
        self._version_history: list[str] = ["v1"]

    @property
    def alias(self) -> str:
        """Current index alias."""
        return self._alias

    @property
    def version(self) -> str:
        """Current index version."""
        return self._version

    @property
    def version_history(self) -> list[str]:
        """History of index versions."""
        return list(self._version_history)

    def index_document(
        self,
        doc_id: str,
        content: str,
        embedding: list[float] | None = None,
        **metadata: object,
    ) -> None:
        """Index a document into both lexical and dense indexes."""
        self._lexical.add(doc_id, content, **metadata)
        if embedding is not None:
            self._dense.add(doc_id, content, embedding, **metadata)

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from both indexes."""
        self._lexical.remove(doc_id)
        self._dense.remove(doc_id)

    def hybrid_search(
        self,
        query: str,
        query_embedding: list[float] | None = None,
        *,
        top_k: int = 10,
        lexical_weight: float = 1.0,
        dense_weight: float = 1.0,
    ) -> list[ScoredDocument]:
        """Run hybrid retrieval: BM25 + dense + RRF + rerank.

        Steps:
        1. BM25 lexical search
        2. Dense vector search (if query_embedding provided)
        3. RRF fusion of both rankings
        4. Rerank the fused results
        """
        lexical_results = self._lexical.search(query, top_k=top_k * 2)

        rankings: list[list[ScoredDocument]] = []
        if lexical_results:
            rankings.append(lexical_results)

        if query_embedding is not None:
            dense_results = self._dense.search(query_embedding, top_k=top_k * 2)
            if dense_results:
                rankings.append(dense_results)

        if not rankings:
            return []

        fused = rankings[0][:top_k] if len(rankings) == 1 else fuse(rankings, top_k=top_k)

        reranked = self._reranker.rerank(query, fused, top_k=top_k)
        return reranked

    def kill_and_rebuild(
        self,
        documents: list[dict[str, Any]],
    ) -> IndexStats:
        """Kill the current index and rebuild from scratch.

        Creates a new version, clears both indexes, bulk-loads documents,
        and switches the alias.  The Source Ledger is never modified.

        Each document dict must have 'doc_id', 'content', and optionally
        'embedding' and any metadata fields.
        """
        # Create new version
        version_num = len(self._version_history) + 1
        new_version = f"v{version_num}"

        # Kill current indexes
        self._lexical.clear()
        self._dense.clear()

        # Bulk load
        for doc in documents:
            doc_id = doc["doc_id"]
            content = doc["content"]
            embedding = doc.get("embedding")
            metadata = {k: v for k, v in doc.items() if k not in ("doc_id", "content", "embedding")}
            self.index_document(doc_id, content, embedding, **metadata)

        # Switch version and alias
        old_alias = self._alias
        self._version = new_version
        self._version_history.append(new_version)
        # Alias stays the same (atomic switch in real OpenSearch)

        return IndexStats(
            version=new_version,
            lexical_count=self._lexical.size,
            dense_count=self._dense.size,
            alias=old_alias,
        )

    def stats(self) -> IndexStats:
        """Return current index statistics."""
        return IndexStats(
            version=self._version,
            lexical_count=self._lexical.size,
            dense_count=self._dense.size,
            alias=self._alias,
        )
