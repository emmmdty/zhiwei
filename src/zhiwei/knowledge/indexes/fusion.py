"""S5 Reciprocal Rank Fusion: merge ranked lists from multiple retrieval legs.

Combines BM25 lexical and dense vector results using the RRF formula:
    score(d) = sum over rankings r of 1 / (k + rank_r(d))

k=60 is the standard constant from the original RRF paper (Cormack et al., 2009).
"""

from __future__ import annotations

from dataclasses import dataclass, field

_RRF_K = 60


@dataclass(frozen=True)
class ScoredDocument:
    """A document with its fused RRF score."""

    doc_id: str
    score: float
    content: str
    metadata: dict[str, object] = field(default_factory=dict)


def rrf_score(rank: int, *, k: int = _RRF_K) -> float:
    """Compute the reciprocal rank contribution for a single ranking.

    Args:
        rank: 1-based rank of the document in a single ranking.
        k: Constant that controls the influence of high ranks.  Default 60.
    """
    return 1.0 / (k + rank)


def fuse(
    rankings: list[list[ScoredDocument]],
    *,
    top_k: int = 10,
    k: int = _RRF_K,
) -> list[ScoredDocument]:
    """Fuse multiple ranked lists into a single RRF ranking.

    Each ranking is a list of ScoredDocument in descending score order.
    Documents that appear in multiple rankings receive contributions from
    each ranking.  Content and metadata are taken from the first ranking
    that contains the document.

    Args:
        rankings: List of ranked result lists (one per retrieval leg).
        top_k: Maximum number of results to return.
        k: RRF constant.  Default 60.

    Returns:
        Fused ranking in descending score order, limited to top_k.
    """
    if not rankings:
        return []

    doc_scores: dict[str, float] = {}
    doc_content: dict[str, str] = {}
    doc_metadata: dict[str, dict[str, object]] = {}

    for ranking in rankings:
        for rank_idx, scored_doc in enumerate(ranking):
            doc_id = scored_doc.doc_id
            contribution = rrf_score(rank_idx + 1, k=k)
            doc_scores[doc_id] = doc_scores.get(doc_id, 0.0) + contribution
            if doc_id not in doc_content:
                doc_content[doc_id] = scored_doc.content
                doc_metadata[doc_id] = scored_doc.metadata

    fused = [
        ScoredDocument(
            doc_id=doc_id,
            score=score,
            content=doc_content[doc_id],
            metadata=doc_metadata[doc_id],
        )
        for doc_id, score in doc_scores.items()
    ]
    fused.sort(key=lambda s: s.score, reverse=True)
    return fused[:top_k]
