"""S5 Knowledge Planner: query planning, candidate generation.

Typed query planning for doc/code/GitHub/DB/cross-source queries.
Score breakdown with SourceVersion/Locator/ACL/freshness.

事实源：S5 spec §5、ADR-006。
"""

from __future__ import annotations

from typing import Any

from zhiwei.knowledge.acl import ACLContext, recheck_after_hydration
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    SourceVersion,
    SourceVersionState,
)
from zhiwei.knowledge.freshness import FreshnessPolicy, FreshnessState, evaluate_freshness
from zhiwei.knowledge.query import (
    EvidenceRequirement,
    KnowledgeQuery,
    QueryCandidate,
    QueryPlan,
    QuerySource,
    ScoreBreakdown,
    SortField,
)


class KnowledgePlannerError(Exception):
    """Base error for Knowledge Planner operations."""


class QueryRoutingError(KnowledgePlannerError):
    """Error during query routing to source-specific strategies."""


class KnowledgePlanner:
    """Plans and executes knowledge queries across sources.

    Responsibilities:
    - Route queries to source-specific strategies
    - Pre-filter candidates by ACL
    - Generate score breakdowns per candidate
    - Re-check ACL after hydration (ADR-006)
    """

    def __init__(
        self,
        *,
        ledger: Any | None = None,
        freshness_policies: dict[str, FreshnessPolicy] | None = None,
        clock: Any = None,
    ) -> None:
        self._ledger = ledger
        self._freshness_policies = freshness_policies or {}
        self._clock = clock

    def plan(self, query: KnowledgeQuery) -> QueryPlan:
        """Generate a query plan based on the KnowledgeQuery.

        Routes to appropriate search strategy based on source types.
        """
        source_type = self._resolve_source_type(query)
        strategy = self._select_strategy(source_type, query)

        return QueryPlan(
            query_id=query.query_id,
            source_type=source_type,
            search_strategy=strategy,
            filters=self._build_filters(query),
            top_k=query.top_k,
            needs_acl_recheck=query.evidence_requirement != EvidenceRequirement.NONE,
            needs_freshness_check=query.evidence_requirement == EvidenceRequirement.FRESH,
        )

    def generate_candidates(
        self,
        query: KnowledgeQuery,
        versions: list[SourceVersion],
        acl_context: ACLContext,
    ) -> list[QueryCandidate]:
        """Generate scored candidates from SourceVersions.

        1. Pre-filter by current ACL
        2. Score each candidate
        3. Re-check ACL after hydration (ADR-006)
        """
        filtered = self._pre_filter(versions, acl_context)

        candidates: list[QueryCandidate] = []
        for version in filtered:
            score = self._score_candidate(version, query, acl_context)
            acl_recheck = recheck_after_hydration(version, acl_context)

            is_revoked = version.state == SourceVersionState.REVOKED
            acl_revoked = acl_recheck.access_revoked

            candidate = QueryCandidate(
                source_version_id=str(version.id),
                source_object_id=str(version.source_object_id),
                connector=version.locator.connector,
                uri=version.locator.uri,
                content_digest=version.content_digest,
                classification=version.classification.value,
                observed_at=version.observed_at,
                score=score,
                is_revoked=is_revoked,
                acl_access_revoked=acl_revoked,
            )
            candidates.append(candidate)

        candidates.sort(
            key=lambda c: self._sort_key(c, query.sort_by),
            reverse=True,
        )

        return candidates[: query.top_k]

    def _resolve_source_type(self, query: KnowledgeQuery) -> QuerySource:
        """Determine the primary source type for routing."""
        if len(query.sources) > 1:
            return QuerySource.CROSS_SOURCE
        if query.sources:
            return query.sources[0]
        return QuerySource.DOC

    def _select_strategy(
        self, source_type: QuerySource, query: KnowledgeQuery
    ) -> str:
        """Select search strategy based on source type and query characteristics."""
        if source_type == QuerySource.CODE:
            if query.exact_identifiers:
                return "exact_path_symbol"
            return "code_bm25"
        if source_type == QuerySource.DB:
            return "schema_grounded_query"
        if source_type == QuerySource.GITHUB:
            return "github_pr_issue_search"
        if source_type == QuerySource.DOC:
            return "doc_bm25_dense_rrf_rerank"
        if source_type == QuerySource.CROSS_SOURCE:
            return "cross_source_fusion"
        return "doc_bm25_dense_rrf_rerank"

    def _build_filters(self, query: KnowledgeQuery) -> dict[str, Any]:
        """Build filter dict from query constraints."""
        filters: dict[str, Any] = {}
        if query.entity_filters:
            filters["entities"] = list(query.entity_filters)
        if query.time_from:
            filters["time_from"] = query.time_from.isoformat()
        if query.time_to:
            filters["time_to"] = query.time_to.isoformat()
        if query.exact_identifiers:
            filters["exact_identifiers"] = query.exact_identifiers
        if query.connector_filter:
            filters["connector"] = query.connector_filter
        return filters

    def _pre_filter(
        self,
        versions: list[SourceVersion],
        acl_context: ACLContext,
    ) -> list[SourceVersion]:
        """Pre-filter candidates by current ACL (ADR-006)."""
        from zhiwei.knowledge.acl import pre_filter

        return pre_filter(versions, acl_context)

    def _score_candidate(
        self,
        version: SourceVersion,
        query: KnowledgeQuery,
        acl_context: ACLContext,
    ) -> ScoreBreakdown:
        """Compute score breakdown for a candidate."""
        acl_score = self._compute_acl_score(version.acl, acl_context)
        freshness_score = self._compute_freshness_score(version, query)
        relevance_score = self._compute_relevance_score(version, query)
        classification_score = self._compute_classification_score(version, query)

        total = (
            0.3 * acl_score
            + 0.2 * freshness_score
            + 0.35 * relevance_score
            + 0.15 * classification_score
        )

        acl_check = recheck_after_hydration(version, acl_context)

        classification_ceiling_order = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}
        ceiling = classification_ceiling_order.get(query.classification_ceiling, 0)
        content = classification_ceiling_order.get(version.classification.value, 0)
        classification_passes = content <= ceiling

        return ScoreBreakdown(
            source_version_id=str(version.id),
            locator_connector=version.locator.connector,
            locator_uri=version.locator.uri,
            acl_score=round(acl_score, 4),
            freshness_score=round(freshness_score, 4),
            relevance_score=round(relevance_score, 4),
            classification_score=round(classification_score, 4),
            total_score=round(total, 4),
            acl_passes_recheck=acl_check.allowed,
            classification_passes=classification_passes,
        )

    def _compute_acl_score(
        self, snapshot: ACLSnapshot, context: ACLContext
    ) -> float:
        """Compute ACL score: 1.0 if allowed, 0.0 otherwise."""
        from zhiwei.knowledge.acl import _check_acl

        result = _check_acl(snapshot, context)
        return 1.0 if result.allowed else 0.0

    def _compute_freshness_score(
        self, version: SourceVersion, query: KnowledgeQuery
    ) -> float:
        """Compute freshness score based on policy."""
        policy = self._freshness_policies.get(version.locator.connector)
        kwargs: dict[str, Any] = {}
        if self._clock is not None:
            kwargs["reference_time"] = self._clock()
        result = evaluate_freshness(version, policy, **kwargs)
        state_scores = {
            FreshnessState.FRESH: 1.0,
            FreshnessState.AGING: 0.7,
            FreshnessState.STALE: 0.3,
            FreshnessState.EXPIRED: 0.0,
        }
        return state_scores.get(result.state, 0.0)

    def _compute_relevance_score(
        self, version: SourceVersion, query: KnowledgeQuery
    ) -> float:
        """Compute relevance score (simplified: based on text presence)."""
        if not query.text:
            return 0.5
        score = 0.5
        for identifier in query.exact_identifiers.values():
            if identifier.lower() in version.locator.uri.lower():
                score = min(1.0, score + 0.3)
        for entity in query.entity_filters:
            if entity.lower() in version.locator.uri.lower():
                score = min(1.0, score + 0.2)
        return score

    def _compute_classification_score(
        self, version: SourceVersion, query: KnowledgeQuery
    ) -> float:
        """Compute classification match score."""
        classification_order = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}
        ceiling = classification_order.get(query.classification_ceiling, 0)
        content = classification_order.get(version.classification.value, 0)
        if content <= ceiling:
            return 1.0
        return 0.0

    def _sort_key(self, candidate: QueryCandidate, sort_by: SortField) -> float:
        """Extract sort key from candidate."""
        if sort_by == SortField.FRESHNESS:
            return candidate.score.freshness_score
        if sort_by == SortField.RELEVANCE:
            return candidate.score.relevance_score
        return candidate.score.total_score
