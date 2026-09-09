"""S7 Memory index: exact / lexical / dense retrieval backends.

In-memory implementations for contract testing; production would back
with OpenSearch + vector store. Each index returns MemoryRecord references
with a single-dimension score for fusion.

事实源：S7 spec §4（retrieval pipeline）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from zhiwei.memory.domain import MemoryRecord


class MemoryIndex(Protocol):
    """Protocol for a memory retrieval index."""

    def search_exact(self, query_key: str, *, top_k: int = 10) -> list[ScoredRecord]: ...

    def search_lexical(self, query_text: str, *, top_k: int = 10) -> list[ScoredRecord]: ...

    def search_dense(
        self, query_embedding: list[float], *, top_k: int = 10
    ) -> list[ScoredRecord]: ...


@dataclass(frozen=True)
class ScoredRecord:
    """A memory record with a single-dimension relevance score."""

    record: MemoryRecord
    score: float
    source: str  # "exact" | "lexical" | "dense"


@dataclass
class ExactIndex:
    """Exact-match index on normalized (key, subject, canonical_value)."""

    _entries: dict[str, MemoryRecord] = field(default_factory=dict)

    def add(self, record: MemoryRecord) -> None:
        norm = _normalize_text(f"{record.key}|{record.subject}|{record.canonical_value}")
        self._entries[norm] = record

    def remove(self, record_id: UUID) -> None:
        to_delete = [k for k, v in self._entries.items() if v.id == record_id]
        for k in to_delete:
            del self._entries[k]

    def search_exact(self, query_key: str, *, top_k: int = 10) -> list[ScoredRecord]:
        norm = _normalize_text(query_key)
        results: list[ScoredRecord] = []
        for entry_norm, record in self._entries.items():
            if norm in entry_norm or entry_norm.startswith(norm):
                results.append(ScoredRecord(record=record, score=1.0, source="exact"))
        results.sort(key=lambda s: s.score, reverse=True)
        return results[:top_k]

    def search_lexical(self, query_text: str, *, top_k: int = 10) -> list[ScoredRecord]:
        return []

    def search_dense(
        self, query_embedding: list[float], *, top_k: int = 10
    ) -> list[ScoredRecord]:
        return []


@dataclass
class LexicalIndex:
    """BM25-style lexical index with token overlap scoring."""

    _documents: list[tuple[UUID, MemoryRecord, list[str]]] = field(default_factory=list)
    _doc_count: int = 0
    _avg_dl: float = 0.0
    _df: dict[str, int] = field(default_factory=dict)

    def add(self, record: MemoryRecord) -> None:
        text = f"{record.subject} {record.key} {record.canonical_value}"
        tokens = _tokenize(text)
        self._documents.append((record.id, record, tokens))
        self._doc_count += 1
        self._avg_dl = (
            (self._avg_dl * (self._doc_count - 1) + len(tokens)) / self._doc_count
        )
        for tok in set(tokens):
            self._df[tok] = self._df.get(tok, 0) + 1

    def remove(self, record_id: UUID) -> None:
        self._documents = [
            (rid, rec, toks) for rid, rec, toks in self._documents if rid != record_id
        ]
        self._doc_count = len(self._documents)
        self._rebuild_df()

    def search_exact(self, query_key: str, *, top_k: int = 10) -> list[ScoredRecord]:
        return []

    def search_lexical(self, query_text: str, *, top_k: int = 10) -> list[ScoredRecord]:
        query_tokens = _tokenize(query_text)
        if not query_tokens or self._doc_count == 0:
            return []

        k1 = 1.5
        b = 0.75
        scores: list[ScoredRecord] = []

        for _doc_id, record, doc_tokens in self._documents:
            score = 0.0
            doc_len = len(doc_tokens)
            doc_tf: dict[str, int] = {}
            for t in doc_tokens:
                doc_tf[t] = doc_tf.get(t, 0) + 1

            for qt in query_tokens:
                tf = doc_tf.get(qt, 0)
                df = self._df.get(qt, 0)
                if tf == 0 or df == 0:
                    continue
                idf = math.log((self._doc_count - df + 0.5) / (df + 0.5) + 1.0)
                tf_norm = (tf * (k1 + 1)) / (
                    tf + k1 * (1 - b + b * doc_len / max(self._avg_dl, 1))
                )
                score += idf * tf_norm

            if score > 0:
                scores.append(ScoredRecord(record=record, score=score, source="lexical"))

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores[:top_k]

    def search_dense(
        self, query_embedding: list[float], *, top_k: int = 10
    ) -> list[ScoredRecord]:
        return []

    def _rebuild_df(self) -> None:
        self._df.clear()
        for _, _, tokens in self._documents:
            for tok in set(tokens):
                self._df[tok] = self._df.get(tok, 0) + 1


@dataclass
class DenseIndex:
    """Cosine-similarity dense index (in-memory, production would use vector store)."""

    _records: list[MemoryRecord] = field(default_factory=list)
    _embeddings: list[list[float]] = field(default_factory=list)

    def add(self, record: MemoryRecord, embedding: list[float]) -> None:
        self._records.append(record)
        self._embeddings.append(embedding)

    def remove(self, record_id: UUID) -> None:
        indices = [i for i, r in enumerate(self._records) if r.id == record_id]
        for i in reversed(indices):
            self._records.pop(i)
            self._embeddings.pop(i)

    def search_exact(self, query_key: str, *, top_k: int = 10) -> list[ScoredRecord]:
        return []

    def search_lexical(self, query_text: str, *, top_k: int = 10) -> list[ScoredRecord]:
        return []

    def search_dense(
        self, query_embedding: list[float], *, top_k: int = 10
    ) -> list[ScoredRecord]:
        if not query_embedding or not self._embeddings:
            return []

        scores: list[ScoredRecord] = []
        for record, emb in zip(self._records, self._embeddings, strict=True):
            sim = _cosine_similarity(query_embedding, emb)
            if sim > 0:
                scores.append(ScoredRecord(record=record, score=sim, source="dense"))

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores[:top_k]


def _normalize_text(text: str) -> str:
    return text.lower().strip()


def _tokenize(text: str) -> list[str]:
    normalized = re.sub(r"[^\w\s]", " ", text.lower())
    return [t for t in normalized.split() if len(t) > 1]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
