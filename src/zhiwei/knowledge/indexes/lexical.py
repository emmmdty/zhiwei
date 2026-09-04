"""S5 BM25 lexical index: term-frequency scoring with source-native exact-match priority.

Pure in-memory BM25 implementation.  No external dependencies beyond stdlib.
k1=1.5, b=0.75 (standard Okapi BM25 parameters).
"""

from __future__ import annotations

import math
import re
from collections import Counter

from zhiwei.knowledge.indexes.fusion import ScoredDocument

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on non-alphanumeric characters."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _idf(doc_freq: int, total_docs: int) -> float:
    """Standard IDF with floor to avoid negative values."""
    return math.log((total_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)


class BM25Index:
    """In-memory BM25 lexical index.

    Stores documents as tokenized term frequency vectors and scores queries
    using the Okapi BM25 formula.
    """

    def __init__(self, *, k1: float = _K1, b: float = _B) -> None:
        self._k1 = k1
        self._b = b
        self._documents: dict[str, str] = {}
        self._doc_tokens: dict[str, list[str]] = {}
        self._doc_term_freq: dict[str, Counter[str]] = {}
        self._doc_len: dict[str, int] = {}
        self._total_docs: int = 0
        self._avg_doc_len: float = 0.0
        self._doc_freq: Counter[str] = Counter()

    @property
    def size(self) -> int:
        """Number of indexed documents."""
        return self._total_docs

    def add(self, doc_id: str, content: str, **metadata: object) -> None:
        """Index a document.  Overwrites if doc_id already exists."""
        if doc_id in self._documents:
            self._remove(doc_id)

        tokens = _tokenize(content)
        tf = Counter(tokens)

        self._documents[doc_id] = content
        self._doc_tokens[doc_id] = tokens
        self._doc_term_freq[doc_id] = tf
        self._doc_len[doc_id] = len(tokens)
        self._total_docs += 1
        self._avg_doc_len = (
            sum(self._doc_len.values()) / self._total_docs
            if self._total_docs > 0
            else 0.0
        )
        for term in tf:
            self._doc_freq[term] += 1

    def _remove(self, doc_id: str) -> None:
        """Remove a document from the index."""
        if doc_id not in self._documents:
            return
        tf = self._doc_term_freq[doc_id]
        for term in tf:
            self._doc_freq[term] -= 1
            if self._doc_freq[term] <= 0:
                del self._doc_freq[term]
        del self._documents[doc_id]
        del self._doc_tokens[doc_id]
        del self._doc_term_freq[doc_id]
        del self._doc_len[doc_id]
        self._total_docs -= 1
        self._avg_doc_len = (
            sum(self._doc_len.values()) / self._total_docs
            if self._total_docs > 0
            else 0.0
        )

    def remove(self, doc_id: str) -> None:
        """Remove a document from the index.  No-op if not found."""
        self._remove(doc_id)

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        """Score all documents against the query and return the top_k results.

        Uses Okapi BM25 scoring.  Returns empty list if no documents match.
        """
        if not self._documents or not query.strip():
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores: list[ScoredDocument] = []
        for doc_id in self._documents:
            score = 0.0
            for term in query_tokens:
                if term not in self._doc_term_freq[doc_id]:
                    continue
                tf = self._doc_term_freq[doc_id][term]
                df = self._doc_freq.get(term, 0)
                idf = _idf(df, self._total_docs)
                dl = self._doc_len[doc_id]
                norm = 1.0 - self._b + self._b * dl / max(self._avg_doc_len, 1.0)
                term_score = idf * (tf * (self._k1 + 1.0)) / (tf + self._k1 * norm)
                score += term_score

            if score > 0:
                scores.append(
                    ScoredDocument(doc_id=doc_id, score=score, content=self._documents[doc_id])
                )

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores[:top_k]

    def clear(self) -> None:
        """Remove all documents from the index."""
        self._documents.clear()
        self._doc_tokens.clear()
        self._doc_term_freq.clear()
        self._doc_len.clear()
        self._doc_freq.clear()
        self._total_docs = 0
        self._avg_doc_len = 0.0
