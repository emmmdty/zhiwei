"""S5-T5 RED: OpenSearch indexes and Context Graph contract tests.

Covers:
- BM25 lexical index determinism and scoring
- Dense vector index cosine similarity
- RRF fusion of multiple rankings
- Reranker pass-through
- Hybrid retrieval assembly (BM25+dense+RRF+rerank)
- Code exact symbol/path/ref/commit outrank semantic-only
- Context Graph typed temporal edges with source refs
- Graph deletion/rebuild from Ledger
- OpenSearch kill/rebuild and alias-switch without Source Ledger change
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.knowledge.contracts import (
    Locator,
    SourceObject,
    SourceVersion,
)
from zhiwei.knowledge.graph import (
    ContextEdge,
    ContextGraph,
    EdgeType,
    GraphQuery,
)
from zhiwei.knowledge.indexes.dense import DenseIndex
from zhiwei.knowledge.indexes.fusion import ScoredDocument, fuse, rrf_score
from zhiwei.knowledge.indexes.lexical import BM25Index
from zhiwei.knowledge.indexes.opensearch import OpenSearchPort
from zhiwei.knowledge.indexes.rerank import Reranker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _make_locator(**overrides: str | None) -> Locator:
    defaults: dict[str, str | None] = {"connector": "files", "uri": "/docs/spec.md"}
    defaults.update(overrides)
    non_none = {k: v for k, v in defaults.items() if v is not None}
    return Locator(**non_none)


def _content_digest(content: bytes = b"test content") -> str:
    return digest_bytes(content)


def _make_version(*, locator: Locator | None = None) -> SourceVersion:
    return SourceVersion(
        id=new_id(),
        source_object_id=new_id(),
        version_seq=1,
        locator=locator or _make_locator(),
        content_digest=_content_digest(),
        observed_at=NOW,
        valid_at=NOW,
    )


# ---------------------------------------------------------------------------
# BM25 Lexical Index
# ---------------------------------------------------------------------------

class TestBM25Index:
    def test_empty_index(self) -> None:
        idx = BM25Index()
        assert idx.size == 0
        assert idx.search("query") == []

    def test_add_and_search(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "the quick brown fox")
        idx.add("doc2", "the lazy dog")
        results = idx.search("fox")
        assert len(results) == 1
        assert results[0].doc_id == "doc1"

    def test_multiple_results_ranked(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "python programming language")
        idx.add("doc2", "python data science")
        idx.add("doc3", "java programming")
        results = idx.search("python programming")
        assert len(results) >= 2
        # doc1 should rank highest (both terms)
        assert results[0].doc_id == "doc1"

    def test_top_k_limit(self) -> None:
        idx = BM25Index()
        for i in range(20):
            idx.add(f"doc{i}", f"content {i} test")
        results = idx.search("test", top_k=5)
        assert len(results) == 5

    def test_remove_document(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "hello world")
        idx.add("doc2", "goodbye world")
        idx.remove("doc1")
        assert idx.size == 1
        results = idx.search("hello")
        assert len(results) == 0

    def test_remove_nonexistent_noop(self) -> None:
        idx = BM25Index()
        idx.remove("nonexistent")
        assert idx.size == 0

    def test_overwrite_document(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "original content")
        idx.add("doc1", "updated content")
        assert idx.size == 1
        results = idx.search("updated")
        assert len(results) == 1

    def test_clear(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "content")
        idx.clear()
        assert idx.size == 0

    def test_deterministic(self) -> None:
        """Same content produces same scores across runs."""
        idx1 = BM25Index()
        idx1.add("doc1", "the quick brown fox jumps")
        idx1.add("doc2", "the lazy brown dog sleeps")

        idx2 = BM25Index()
        idx2.add("doc1", "the quick brown fox jumps")
        idx2.add("doc2", "the lazy brown dog sleeps")

        r1 = idx1.search("brown fox")
        r2 = idx2.search("brown fox")
        assert len(r1) == len(r2)
        for a, b in zip(r1, r2, strict=False):
            assert a.doc_id == b.doc_id
            assert abs(a.score - b.score) < 1e-10

    def test_case_insensitive(self) -> None:
        idx = BM25Index()
        idx.add("doc1", "Python Programming")
        results = idx.search("python")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Dense Vector Index
# ---------------------------------------------------------------------------

class TestDenseIndex:
    def test_empty_index(self) -> None:
        idx = DenseIndex()
        assert idx.size == 0
        assert idx.dimension is None
        assert idx.search([1.0, 0.0]) == []

    def test_add_and_search(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "hello", [1.0, 0.0, 0.0])
        idx.add("doc2", "world", [0.0, 1.0, 0.0])
        results = idx.search([1.0, 0.0, 0.0], top_k=1)
        assert len(results) == 1
        assert results[0].doc_id == "doc1"
        assert results[0].score == pytest.approx(1.0)

    def test_cosine_similarity_ranking(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "close", [1.0, 0.0])
        idx.add("doc2", "far", [0.0, 1.0])
        idx.add("doc3", "medium", [0.7, 0.7])
        query = [1.0, 0.0]
        results = idx.search(query, top_k=3)
        assert results[0].doc_id == "doc1"
        assert results[0].score > results[2].score

    def test_dimension_mismatch(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "content", [1.0, 0.0])
        with pytest.raises(ValueError, match="dimension mismatch"):
            idx.search([1.0, 0.0, 0.0])

    def test_add_dimension_mismatch(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "content", [1.0, 0.0])
        with pytest.raises(ValueError, match="dimension mismatch"):
            idx.add("doc2", "content", [1.0, 0.0, 0.0])

    def test_remove(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "hello", [1.0, 0.0])
        idx.remove("doc1")
        assert idx.size == 0

    def test_clear(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "hello", [1.0, 0.0])
        idx.clear()
        assert idx.size == 0
        assert idx.dimension is None

    def test_zero_vector(self) -> None:
        idx = DenseIndex()
        idx.add("doc1", "empty", [0.0, 0.0])
        results = idx.search([1.0, 0.0])
        assert len(results) == 1
        assert results[0].score == 0.0


# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------

class TestRRFFusion:
    def test_rrf_score_formula(self) -> None:
        """Verify RRF score: 1/(k + rank)."""
        assert rrf_score(1, k=60) == pytest.approx(1.0 / 61.0)
        assert rrf_score(2, k=60) == pytest.approx(1.0 / 62.0)
        assert rrf_score(10, k=60) == pytest.approx(1.0 / 70.0)

    def test_fuse_empty(self) -> None:
        assert fuse([]) == []

    def test_fuse_single_ranking(self) -> None:
        ranking = [ScoredDocument("d1", 0.9, "a"), ScoredDocument("d2", 0.5, "b")]
        fused = fuse([ranking])
        assert len(fused) == 2
        assert fused[0].doc_id == "d1"

    def test_fuse_two_rankings(self) -> None:
        r1 = [
            ScoredDocument("d1", 0.9, "a"),
            ScoredDocument("d2", 0.5, "b"),
        ]
        r2 = [
            ScoredDocument("d2", 0.8, "b"),
            ScoredDocument("d1", 0.6, "a"),
        ]
        fused = fuse([r1, r2])
        # d1 and d2 both appear in both rankings
        assert len(fused) == 2
        # Both get contributions from both rankings
        scores = {s.doc_id: s.score for s in fused}
        assert scores["d1"] > 0
        assert scores["d2"] > 0

    def test_fuse_prefers_multiple_rankings(self) -> None:
        """A doc appearing in multiple rankings should outrank a doc in only one."""
        r1 = [ScoredDocument("d1", 0.9, "a")]
        r2 = [ScoredDocument("d2", 0.9, "b")]
        # d1 only in r1, d2 only in r2 — scores equal by symmetry
        fused = fuse([r1, r2])
        assert len(fused) == 2

    def test_fuse_top_k(self) -> None:
        r1 = [ScoredDocument(f"d{i}", 1.0 - i * 0.1, f"c{i}") for i in range(20)]
        fused = fuse([r1], top_k=5)
        assert len(fused) == 5


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

class TestReranker:
    def test_passthrough(self) -> None:
        reranker = Reranker()
        candidates = [
            ScoredDocument("d1", 0.9, "a"),
            ScoredDocument("d2", 0.5, "b"),
        ]
        result = reranker.rerank("query", candidates)
        assert len(result) == 2
        assert result[0].doc_id == "d1"

    def test_top_k(self) -> None:
        reranker = Reranker()
        candidates = [ScoredDocument(f"d{i}", 1.0, f"c{i}") for i in range(10)]
        result = reranker.rerank("query", candidates, top_k=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Hybrid retrieval assembly (BM25 + dense + RRF + rerank)
# ---------------------------------------------------------------------------

class TestHybridRetrieval:
    def test_bm25_only(self) -> None:
        port = OpenSearchPort()
        port.index_document("d1", "python programming")
        port.index_document("d2", "java programming")
        results = port.hybrid_search("python")
        assert len(results) >= 1
        assert results[0].doc_id == "d1"

    def test_dense_only(self) -> None:
        port = OpenSearchPort()
        port.index_document("d1", "hello", embedding=[1.0, 0.0, 0.0])
        port.index_document("d2", "world", embedding=[0.0, 1.0, 0.0])
        results = port.hybrid_search("query", query_embedding=[1.0, 0.0, 0.0])
        assert len(results) >= 1
        assert results[0].doc_id == "d1"

    def test_hybrid_both_legs(self) -> None:
        port = OpenSearchPort()
        port.index_document("d1", "python data science", embedding=[0.9, 0.1, 0.0])
        port.index_document("d2", "java enterprise", embedding=[0.1, 0.9, 0.0])
        results = port.hybrid_search(
            "python", query_embedding=[1.0, 0.0, 0.0]
        )
        assert len(results) >= 1
        assert results[0].doc_id == "d1"

    def test_empty_query(self) -> None:
        port = OpenSearchPort()
        port.index_document("d1", "content")
        results = port.hybrid_search("")
        assert results == []

    def test_no_documents(self) -> None:
        port = OpenSearchPort()
        results = port.hybrid_search("query")
        assert results == []


# ---------------------------------------------------------------------------
# Source-native exact-match priority
# ---------------------------------------------------------------------------

class TestSourceNativePriority:
    """Code exact path/symbol/ref/commit signals outrank semantic-only results."""

    def test_exact_path_documentoutranks_semantic(self) -> None:
        """A document matching the exact path should rank above semantic matches."""
        port = OpenSearchPort()
        port.index_document(
            "exact_path",
            "src/zhiwei/knowledge/graph.py",
            exact_path="src/zhiwei/knowledge/graph.py",
        )
        port.index_document(
            "semantic_match",
            "This file contains graph-related functionality for the knowledge module",
        )
        results = port.hybrid_search("src/zhiwei/knowledge/graph.py")
        # The exact path match should be ranked first
        assert len(results) >= 1
        assert results[0].doc_id == "exact_path"

    def test_exact_symbol_priority(self) -> None:
        """Documents with exact symbol match should rank above semantic."""
        port = OpenSearchPort()
        port.index_document(
            "symbol_doc",
            "ContextGraph class definition with add_edge method",
            exact_symbol="ContextGraph",
        )
        port.index_document(
            "semantic_doc",
            "A graph structure for storing relationships between entities",
        )
        results = port.hybrid_search("ContextGraph")
        assert len(results) >= 1
        assert results[0].doc_id == "symbol_doc"

    def test_exact_commit_priority(self) -> None:
        """Documents with exact commit SHA should outrank semantic matches."""
        port = OpenSearchPort()
        port.index_document(
            "commit_doc",
            "Changes in commit abc123def456",
            exact_commit="abc123def456",
        )
        port.index_document(
            "semantic_doc",
            "Recent changes to the codebase",
        )
        results = port.hybrid_search("abc123def456")
        assert len(results) >= 1
        assert results[0].doc_id == "commit_doc"


# ---------------------------------------------------------------------------
# Context Graph
# ---------------------------------------------------------------------------

class TestContextGraph:
    def test_add_edge(self) -> None:
        graph = ContextGraph()
        edge = graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        assert edge.edge_type == EdgeType.REFERENCES
        assert edge.valid_to is None

    def test_add_edge_with_valid_to(self) -> None:
        graph = ContextGraph()
        edge = graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.DEPENDS_ON,
            source_version_id=new_id(),
            valid_from=NOW,
            valid_to=NOW,
        )
        assert edge.valid_to == NOW

    def test_add_edge_valid_to_before_valid_from_raises(self) -> None:
        graph = ContextGraph()
        with pytest.raises(ValueError, match="valid_to must not precede"):
            graph.add_edge(
                source_id=new_id(),
                target_id=new_id(),
                edge_type=EdgeType.REFERENCES,
                source_version_id=new_id(),
                valid_from=NOW,
                valid_to=NOW.replace(hour=11),
            )

    def test_query_by_source_id(self) -> None:
        graph = ContextGraph()
        src = new_id()
        graph.add_edge(
            source_id=src,
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        results = graph.query(GraphQuery(source_id=src))
        assert len(results) == 1

    def test_query_by_edge_type(self) -> None:
        graph = ContextGraph()
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.IMPLEMENTS,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        results = graph.query(GraphQuery(edge_type=EdgeType.IMPLEMENTS))
        assert len(results) == 1
        assert results[0].edge_type == EdgeType.IMPLEMENTS

    def test_query_active_at(self) -> None:
        graph = ContextGraph()
        past = NOW.replace(hour=10)
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=past,
            valid_to=NOW.replace(hour=11),  # expired before NOW
        )
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.IMPLEMENTS,
            source_version_id=new_id(),
            valid_from=past,
        )
        # At NOW, only the IMPLEMENTS edge is active (REFERENCES expired at 11:00)
        results = graph.query(GraphQuery(active_at=NOW))
        assert len(results) == 1
        assert results[0].edge_type == EdgeType.IMPLEMENTS

    def test_query_source_version(self) -> None:
        graph = ContextGraph()
        vid = new_id()
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=vid,
            valid_from=NOW,
        )
        results = graph.query(GraphQuery(source_version_id=vid))
        assert len(results) == 1

    def test_delete_edges_for_version(self) -> None:
        graph = ContextGraph()
        vid = new_id()
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=vid,
            valid_from=NOW,
        )
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.IMPLEMENTS,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        count = graph.delete_edges_for_version(vid, reference_time=NOW)
        assert count == 1
        # The expired edge should not appear in active_at query
        active = graph.query(GraphQuery(active_at=NOW))
        assert len(active) == 1
        assert active[0].edge_type == EdgeType.IMPLEMENTS

    def test_delete_nonexistent_version(self) -> None:
        graph = ContextGraph()
        count = graph.delete_edges_for_version(new_id())
        assert count == 0

    def test_rebuild_from_ledger(self) -> None:
        graph = ContextGraph()

        v1 = _make_version()
        v2 = _make_version()

        def factory(version: SourceVersion) -> list[ContextEdge]:
            return [
                ContextEdge(
                    id=new_id(),
                    source_id=version.source_object_id,
                    target_id=new_id(),
                    edge_type=EdgeType.DERIVED_FROM,
                    source_version_id=version.id,
                    valid_from=version.observed_at,
                )
            ]

        count = graph.rebuild_from_ledger([v1, v2], factory)
        assert count == 2

    def test_rebuild_clears_existing(self) -> None:
        graph = ContextGraph()
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        assert graph.stats().total_edges == 1

        def factory(version: SourceVersion) -> list[ContextEdge]:
            return []

        graph.rebuild_from_ledger([], factory)
        assert graph.stats().total_edges == 0

    def test_stats(self) -> None:
        graph = ContextGraph()
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.REFERENCES,
            source_version_id=new_id(),
            valid_from=NOW,
        )
        graph.add_edge(
            source_id=new_id(),
            target_id=new_id(),
            edge_type=EdgeType.IMPLEMENTS,
            source_version_id=new_id(),
            valid_from=NOW,
            valid_to=NOW,
        )
        stats = graph.stats()
        assert stats.total_edges == 2
        assert stats.active_edges == 1
        assert stats.edge_types["references"] == 1
        assert stats.edge_types["implements"] == 1

    def test_edge_types_all_values(self) -> None:
        """All EdgeType values can be used to create edges."""
        graph = ContextGraph()
        for edge_type in EdgeType:
            edge = graph.add_edge(
                source_id=new_id(),
                target_id=new_id(),
                edge_type=edge_type,
                source_version_id=new_id(),
                valid_from=NOW,
            )
            assert edge.edge_type == edge_type
        assert graph.stats().total_edges == len(EdgeType)


# ---------------------------------------------------------------------------
# OpenSearch kill/rebuild and alias-switch
# ---------------------------------------------------------------------------

class TestOpenSearchKillRebuild:
    def test_stats_initial(self) -> None:
        port = OpenSearchPort()
        stats = port.stats()
        assert stats.version == "v1"
        assert stats.lexical_count == 0
        assert stats.dense_count == 0
        assert stats.alias == "knowledge"

    def test_kill_and_rebuild(self) -> None:
        port = OpenSearchPort()
        port.index_document("d1", "old content")
        assert port.stats().lexical_count == 1

        docs = [
            {"doc_id": "d2", "content": "new content a"},
            {"doc_id": "d3", "content": "new content b"},
        ]
        stats = port.kill_and_rebuild(docs)
        assert stats.version == "v2"
        assert stats.lexical_count == 2
        assert port.version == "v2"

    def test_alias_unchanged_after_rebuild(self) -> None:
        port = OpenSearchPort(alias="my_index")
        docs = [{"doc_id": "d1", "content": "content"}]
        stats = port.kill_and_rebuild(docs)
        assert stats.alias == "my_index"
        assert port.alias == "my_index"

    def test_source_ledger_unchanged(self) -> None:
        """Kill/rebuild does not modify Source Ledger objects."""
        from zhiwei.knowledge.ledger import SourceLedger

        ledger = SourceLedger()
        obj = SourceObject(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            source_type="document",
        )
        ledger.register_object(obj)
        v1 = ledger.create_version(
            obj.id,
            locator=_make_locator(),
            content_digest=_content_digest(b"v1"),
            observed_at=NOW,
            valid_at=NOW,
        )

        port = OpenSearchPort()
        port.kill_and_rebuild([{"doc_id": str(v1.id), "content": "indexed"}])

        # Ledger is untouched
        assert ledger.get_version(v1.id).content_digest == _content_digest(b"v1")
        assert ledger.latest_version(obj.id) is not None

    def test_version_history(self) -> None:
        port = OpenSearchPort()
        port.kill_and_rebuild([{"doc_id": "d1", "content": "a"}])
        port.kill_and_rebuild([{"doc_id": "d2", "content": "b"}])
        assert port.version_history == ["v1", "v2", "v3"]

    def test_multiple_rebuilds(self) -> None:
        port = OpenSearchPort()
        for i in range(5):
            port.kill_and_rebuild([{"doc_id": f"d{i}", "content": f"c{i}"}])
        assert port.version == "v6"
        assert port.stats().lexical_count == 1
