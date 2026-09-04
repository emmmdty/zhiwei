"""S7 Memory retrieval: hard filters + exact/lexical/dense + rerank.

硬过滤 org/workspace/scope_subject/ACL/sensitivity/status/time/allowed_profile。
Results carry reason, provenance, conflicts, freshness.

事实源：S7 spec §4（retrieval pipeline）、ADR-009。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from zhiwei.contracts.time import ensure_utc
from zhiwei.memory.candidates import CandidateQueue
from zhiwei.memory.conflicts import ConflictDetector
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    SensitivityLevel,
)
from zhiwei.memory.index import (
    DenseIndex,
    ExactIndex,
    LexicalIndex,
    ScoredRecord,
)


class FilterStatus(StrEnum):
    """Outcome of hard filter evaluation."""

    PASS = "pass"
    REJECTED_ORG = "rejected_org"
    REJECTED_WORKSPACE = "rejected_workspace"
    REJECTED_SCOPE_SUBJECT = "rejected_scope_subject"
    REJECTED_ACL = "rejected_acl"
    REJECTED_SENSITIVITY = "rejected_sensitivity"
    REJECTED_STATUS = "rejected_status"
    REJECTED_TIME = "rejected_time"
    REJECTED_PROFILE = "rejected_profile"


@dataclass(frozen=True)
class HardFilters:
    """Hard filter criteria applied before ranking.

    Every field is optional; None means "no constraint".
    """

    organization_id: UUID | None = None
    workspace_id: UUID | None = None
    scope_subject_id: UUID | None = None
    allowed_principals: frozenset[str] = field(default_factory=frozenset)
    max_sensitivity: SensitivityLevel | None = None
    allowed_statuses: frozenset[MemoryStatus] | None = None
    valid_at: datetime | None = None
    allowed_profile_refs: frozenset[str] | None = None
    exclude_tombstones: bool = True


@dataclass(frozen=True)
class RetrievalResult:
    """A single retrieval result with full provenance."""

    record: MemoryRecord
    score: float
    reason: str
    provenance: tuple[str, ...]
    conflicts: tuple[UUID, ...]
    freshness_seconds: float
    filter_status: FilterStatus


@dataclass(frozen=True)
class RetrievalResponse:
    """Complete retrieval response with ranked results and metadata."""

    results: tuple[RetrievalResult, ...]
    total_scanned: int
    total_passed: int
    query_time_ms: float


def apply_hard_filters(
    record: MemoryRecord,
    filters: HardFilters,
) -> FilterStatus:
    """Apply hard filters to a single record.

    Returns the filter status: PASS if record passes all filters,
    or the first rejected reason.
    """
    if filters.organization_id is not None and record.organization_id != filters.organization_id:
        return FilterStatus.REJECTED_ORG

    if filters.workspace_id is not None and record.workspace_id != filters.workspace_id:
        return FilterStatus.REJECTED_WORKSPACE

    if (
        filters.scope_subject_id is not None
        and record.scope_subject_id != filters.scope_subject_id
    ):
        return FilterStatus.REJECTED_SCOPE_SUBJECT

    # ACL check: principal must be in allowed_principals or scope is user-owner
    if filters.allowed_principals:
        author_str = str(record.author_ref)
        scope_subject_str = str(record.scope_subject_id)
        has_access = (
            author_str in filters.allowed_principals
            or scope_subject_str in filters.allowed_principals
        )
        if not has_access and record.scope != MemoryScope.USER:
            return FilterStatus.REJECTED_ACL

    if filters.max_sensitivity is not None:
        sensitivity_order = {
            SensitivityLevel.LOW: 0,
            SensitivityLevel.MEDIUM: 1,
            SensitivityLevel.HIGH: 2,
        }
        if sensitivity_order.get(record.sensitivity, 0) > sensitivity_order.get(
            filters.max_sensitivity, 2
        ):
            return FilterStatus.REJECTED_SENSITIVITY

    if filters.allowed_statuses is not None and record.status not in filters.allowed_statuses:
        return FilterStatus.REJECTED_STATUS

    if filters.valid_at is not None:
        now_utc = ensure_utc(filters.valid_at)
        if record.valid_from is not None and record.valid_from > now_utc:
            return FilterStatus.REJECTED_TIME
        if record.valid_to is not None and record.valid_to < now_utc:
            return FilterStatus.REJECTED_TIME

    if (
        filters.allowed_profile_refs is not None
        and record.allowed_profile_refs
        and not frozenset(record.allowed_profile_refs) & filters.allowed_profile_refs
    ):
        return FilterStatus.REJECTED_PROFILE

    if filters.exclude_tombstones and record.tombstone:
        return FilterStatus.REJECTED_STATUS

    return FilterStatus.PASS


class MemoryRetriever:
    """Orchestrates hard filtering + multi-stage retrieval + rerank.

    Pipeline:
    1. Hard filter on all records
    2. Exact index lookup
    3. Lexical (BM25-style) search
    4. Dense (cosine similarity) search
    5. Reciprocal Rank Fusion
    6. Conflict projection and freshness annotation
    """

    def __init__(
        self,
        exact_index: ExactIndex | None = None,
        lexical_index: LexicalIndex | None = None,
        dense_index: DenseIndex | None = None,
        queue: CandidateQueue | None = None,
        conflict_detector: ConflictDetector | None = None,
    ) -> None:
        self._exact = exact_index or ExactIndex()
        self._lexical = lexical_index or LexicalIndex()
        self._dense = dense_index or DenseIndex()
        self._queue = queue or CandidateQueue()
        self._conflict_detector = conflict_detector or ConflictDetector()

    def index_record(self, record: MemoryRecord) -> None:
        """Add a record to all indexes."""
        self._exact.add(record)
        self._lexical.add(record)

    def index_record_dense(self, record: MemoryRecord, embedding: list[float]) -> None:
        """Add a record to the dense index with a pre-computed embedding."""
        self._dense.add(record, embedding)

    def remove_record(self, record_id: UUID) -> None:
        """Remove a record from all indexes."""
        self._exact.remove(record_id)
        self._lexical.remove(record_id)
        self._dense.remove(record_id)

    def retrieve(
        self,
        query_text: str,
        filters: HardFilters,
        *,
        query_key: str | None = None,
        query_embedding: list[float] | None = None,
        top_k: int = 10,
        now: datetime | None = None,
    ) -> RetrievalResponse:
        """Execute the full retrieval pipeline.

        1. Collect candidates from all index legs
        2. Apply hard filters
        3. Fuse scores via RRF
        4. Annotate with conflicts and freshness
        5. Return ranked results
        """
        now_utc = ensure_utc(now) if now else ensure_utc(datetime.now(tz=UTC))
        all_candidates: list[ScoredRecord] = []

        # Stage 1-4: Gather candidates from all index legs
        if query_key:
            all_candidates.extend(self._exact.search_exact(query_key, top_k=top_k * 3))
        all_candidates.extend(self._lexical.search_lexical(query_text, top_k=top_k * 3))
        if query_embedding:
            all_candidates.extend(self._dense.search_dense(query_embedding, top_k=top_k * 3))

        total_scanned = len(all_candidates)

        # Stage 5: Hard filter
        filtered: list[RetrievalResult] = []
        for scored in all_candidates:
            status = apply_hard_filters(scored.record, filters)
            if status == FilterStatus.PASS:
                # Compute freshness
                freshness = (now_utc - ensure_utc(scored.record.observed_at)).total_seconds()

                # Detect conflicts
                conflict_ids = self._detect_conflicts_for_record(scored.record)

                result = RetrievalResult(
                    record=scored.record,
                    score=scored.score,
                    reason=f"matched via {scored.source}",
                    provenance=(scored.source,),
                    conflicts=tuple(conflict_ids),
                    freshness_seconds=freshness,
                    filter_status=status,
                )
                filtered.append(result)

        # Stage 6: RRF fusion on filtered results
        fused = self._rrf_fuse(filtered, top_k=top_k)

        total_passed = len(filtered)
        return RetrievalResponse(
            results=tuple(fused),
            total_scanned=total_scanned,
            total_passed=total_passed,
            query_time_ms=0.0,
        )

    def _detect_conflicts_for_record(self, record: MemoryRecord) -> list[UUID]:
        """Find unresolved conflicts involving this record."""
        conflicts = self._conflict_detector.get_unresolved_conflicts()
        return [
            c.conflict_id
            for c in conflicts
            if c.record_a_id == record.id or c.record_b_id == record.id
        ]

    @staticmethod
    def _rrf_fuse(
        results: list[RetrievalResult], *, top_k: int = 10, k: int = 60
    ) -> list[RetrievalResult]:
        """Reciprocal Rank Fusion across retrieval legs.

        Groups results by record id, combines scores by RRF formula.
        """
        by_record: dict[UUID, list[RetrievalResult]] = {}
        for r in results:
            by_record.setdefault(r.record.id, []).append(r)

        fused: list[RetrievalResult] = []
        for _rid, group in by_record.items():
            # Sort group by score within each leg
            group.sort(key=lambda x: x.score, reverse=True)
            rrf_score = 0.0
            best = group[0]
            all_provenance: set[str] = set()
            all_conflicts: set[UUID] = set()
            for rank_idx, r in enumerate(group):
                rrf_score += 1.0 / (k + rank_idx + 1)
                all_provenance.update(r.provenance)
                all_conflicts.update(r.conflicts)

            fused.append(
                RetrievalResult(
                    record=best.record,
                    score=rrf_score,
                    reason=best.reason,
                    provenance=tuple(sorted(all_provenance)),
                    conflicts=tuple(sorted(all_conflicts)),
                    freshness_seconds=best.freshness_seconds,
                    filter_status=FilterStatus.PASS,
                )
            )

        fused.sort(key=lambda r: r.score, reverse=True)
        return fused[:top_k]
