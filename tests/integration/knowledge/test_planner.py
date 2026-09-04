"""S5 Integration: Knowledge Planner end-to-end tests.

验证：
- Query planning across doc/code/GitHub/DB/cross-source
- Score breakdown with SourceVersion/Locator/ACL/freshness
- ACL pre-filter + hydration re-check
- Retrieve handler: Task input → Knowledge Activity → typed candidates
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from zhiwei.knowledge.acl import ACLContext
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceVersion,
    SourceVersionState,
)
from zhiwei.knowledge.freshness import FreshnessPolicy
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import (
    EvidenceRequirement,
    KnowledgeQuery,
    QuerySource,
    SortField,
)
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.retrieve import RetrieveHandler
from zhiwei.workflows.activities.knowledge import (
    KnowledgeActivity,
    KnowledgeActivityInput,
)


def _make_version(
    *,
    connector: str = "test",
    uri: str = "test://doc/1",
    classification: Classification = Classification.PUBLIC,
    state: SourceVersionState = SourceVersionState.ACTIVE,
    acl: ACLSnapshot | None = None,
    observed_at: datetime | None = None,
) -> SourceVersion:
    return SourceVersion(
        id=uuid4(),
        source_object_id=uuid4(),
        version_seq=1,
        locator=Locator(connector=connector, uri=uri),
        content_digest="sha256:" + "a" * 64,
        observed_at=observed_at or datetime(2026, 1, 1, tzinfo=UTC),
        valid_at=observed_at or datetime(2026, 1, 1, tzinfo=UTC),
        acl=acl or ACLSnapshot(),
        classification=classification,
        state=state,
    )


def _make_query(
    *,
    query_id: str = "q1",
    sources: tuple[QuerySource, ...] = (QuerySource.DOC,),
    text: str = "test query",
    top_k: int = 10,
    classification_ceiling: str = "PUBLIC",
    evidence_requirement: EvidenceRequirement = EvidenceRequirement.ANY,
    sort_by: SortField = SortField.SCORE,
) -> KnowledgeQuery:
    return KnowledgeQuery(
        query_id=query_id,
        organization_id=uuid4(),
        workspace_id=uuid4(),
        principal_id=uuid4(),
        text=text,
        sources=sources,
        top_k=top_k,
        classification_ceiling=classification_ceiling,
        evidence_requirement=evidence_requirement,
        sort_by=sort_by,
    )


def _make_acl_ctx(
    *,
    principal_id=None,
    organization_id=None,
    workspace_id=None,
    allowed_principals: frozenset[str] | None = None,
    allowed_groups: frozenset[str] | None = None,
    denied_principals: frozenset[str] | None = None,
) -> ACLContext:
    return ACLContext(
        principal_id=principal_id or uuid4(),
        organization_id=organization_id or uuid4(),
        workspace_id=workspace_id or uuid4(),
        allowed_principals=allowed_principals or frozenset(),
        allowed_groups=allowed_groups or frozenset(),
        denied_principals=denied_principals or frozenset(),
    )


class TestQueryPlanning:
    """Test query routing to appropriate strategies."""

    def test_doc_source_plans_bm25_dense(self):
        planner = KnowledgePlanner()
        query = _make_query(sources=(QuerySource.DOC,))
        plan = planner.plan(query)
        assert plan.source_type == QuerySource.DOC
        assert "bm25" in plan.search_strategy

    def test_code_source_plans_exact_or_bm25(self):
        planner = KnowledgePlanner()
        query = _make_query(sources=(QuerySource.CODE,))
        plan = planner.plan(query)
        assert plan.source_type == QuerySource.CODE

    def test_code_with_exact_identifiers(self):
        planner = KnowledgePlanner()
        query = _make_query(
            sources=(QuerySource.CODE,),
            text="",
        )
        query = query.model_copy(
            update={"exact_identifiers": {"path": "src/main.py"}}
        )
        plan = planner.plan(query)
        assert "exact" in plan.search_strategy

    def test_db_source_plans_schema_grounded(self):
        planner = KnowledgePlanner()
        query = _make_query(sources=(QuerySource.DB,))
        plan = planner.plan(query)
        assert plan.source_type == QuerySource.DB
        assert "schema" in plan.search_strategy

    def test_cross_source_plans_fusion(self):
        planner = KnowledgePlanner()
        query = _make_query(sources=(QuerySource.DOC, QuerySource.CODE))
        plan = planner.plan(query)
        assert plan.source_type == QuerySource.CROSS_SOURCE
        assert "fusion" in plan.search_strategy

    def test_github_source_plans_pr_issue(self):
        planner = KnowledgePlanner()
        query = _make_query(sources=(QuerySource.GITHUB,))
        plan = planner.plan(query)
        assert plan.source_type == QuerySource.GITHUB
        assert "github" in plan.search_strategy


class TestCandidateGeneration:
    """Test candidate scoring and ACL enforcement."""

    def test_empty_versions_returns_empty(self):
        planner = KnowledgePlanner()
        query = _make_query()
        ctx = _make_acl_ctx()
        candidates = planner.generate_candidates(query, [], ctx)
        assert candidates == []

    def test_allowed_principal_gets_candidate(self):
        pid = uuid4()
        version = _make_version(acl=ACLSnapshot(allowed_principals=(str(pid),)))
        planner = KnowledgePlanner()
        query = _make_query()
        ctx = _make_acl_ctx(principal_id=pid)
        candidates = planner.generate_candidates(query, [version], ctx)
        assert len(candidates) == 1
        assert candidates[0].score.acl_passes_recheck is True

    def test_denied_principal_excluded(self):
        pid = uuid4()
        version = _make_version(
            acl=ACLSnapshot(
                allowed_principals=(str(pid),),
                denied_principals=(str(pid),),
            )
        )
        planner = KnowledgePlanner()
        query = _make_query()
        ctx = _make_acl_ctx(principal_id=pid)
        candidates = planner.generate_candidates(query, [version], ctx)
        assert len(candidates) == 0

    def test_top_k_limits_results(self):
        versions = [
            _make_version(uri=f"test://doc/{i}")
            for i in range(20)
        ]
        planner = KnowledgePlanner()
        query = _make_query(top_k=5)
        ctx = _make_acl_ctx()
        candidates = planner.generate_candidates(query, versions, ctx)
        assert len(candidates) <= 5

    def test_score_breakdown_computed(self):
        pid = uuid4()
        version = _make_version(acl=ACLSnapshot(allowed_principals=(str(pid),)))
        planner = KnowledgePlanner()
        query = _make_query()
        ctx = _make_acl_ctx(principal_id=pid)
        candidates = planner.generate_candidates(query, [version], ctx)
        assert len(candidates) == 1
        score = candidates[0].score
        assert 0.0 <= score.acl_score <= 1.0
        assert 0.0 <= score.freshness_score <= 1.0
        assert 0.0 <= score.relevance_score <= 1.0
        assert 0.0 <= score.classification_score <= 1.0
        assert 0.0 <= score.total_score <= 1.0

    def test_revoked_version_flagged(self):
        version = _make_version(state=SourceVersionState.REVOKED)
        planner = KnowledgePlanner()
        query = _make_query()
        ctx = _make_acl_ctx()
        candidates = planner.generate_candidates(query, [version], ctx)
        # Revoked version is filtered out by pre-filter (ACL check returns unknown)
        assert len(candidates) == 0


class TestFreshnessIntegration:
    """Test freshness scoring with policies."""

    def test_fresh_version_scores_high(self):
        pid = uuid4()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        version = _make_version(
            acl=ACLSnapshot(allowed_principals=(str(pid),)),
            observed_at=now - timedelta(days=1),
        )
        policy = FreshnessPolicy(connector="test", aging_threshold=timedelta(days=7))
        planner = KnowledgePlanner(
            freshness_policies={"test": policy},
            clock=lambda: now,
        )
        query = _make_query()
        ctx = _make_acl_ctx(principal_id=pid)
        candidates = planner.generate_candidates(query, [version], ctx)
        assert len(candidates) == 1
        assert candidates[0].score.freshness_score == 1.0

    def test_old_version_scores_lower(self):
        pid = uuid4()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        version = _make_version(
            acl=ACLSnapshot(allowed_principals=(str(pid),)),
            observed_at=now - timedelta(days=20),
        )
        policy = FreshnessPolicy(connector="test", aging_threshold=timedelta(days=7))
        planner = KnowledgePlanner(
            freshness_policies={"test": policy},
            clock=lambda: now,
        )
        query = _make_query()
        ctx = _make_acl_ctx(principal_id=pid)
        candidates = planner.generate_candidates(query, [version], ctx)
        assert len(candidates) == 1
        assert candidates[0].score.freshness_score < 1.0


class TestRetrieveHandler:
    """Test Retrieve handler: Task input → typed candidates."""

    def test_missing_query_returns_error(self):
        handler = RetrieveHandler()
        input = TaskInput(
            task_id="t1",
            attempt_id=uuid4(),
            input_values={"acl": {"principal_id": str(uuid4())}},
        )
        output = handler.execute(input)
        assert output.output_values["status"] == "error"
        assert "missing query" in output.output_values["error"]

    def test_missing_acl_returns_error(self):
        handler = RetrieveHandler()
        input = TaskInput(
            task_id="t1",
            attempt_id=uuid4(),
            input_values={
                "query": {
                    "query_id": "q1",
                    "organization_id": str(uuid4()),
                    "workspace_id": str(uuid4()),
                    "principal_id": str(uuid4()),
                }
            },
        )
        output = handler.execute(input)
        assert output.output_values["status"] == "error"
        assert "missing acl" in output.output_values["error"]

    def test_valid_query_returns_candidates(self):
        pid = uuid4()
        org = uuid4()
        ws = uuid4()
        version = _make_version(acl=ACLSnapshot(allowed_principals=(str(pid),)))

        handler = RetrieveHandler()
        input = TaskInput(
            task_id="t1",
            attempt_id=uuid4(),
            input_values={
                "query": {
                    "query_id": "q1",
                    "organization_id": str(org),
                    "workspace_id": str(ws),
                    "principal_id": str(pid),
                    "text": "test",
                    "sources": ["doc"],
                    "top_k": 10,
                },
                "acl": {
                    "principal_id": str(pid),
                    "organization_id": str(org),
                    "workspace_id": str(ws),
                },
                "candidates": [
                    {
                        "id": str(version.id),
                        "source_object_id": str(version.source_object_id),
                        "version_seq": 1,
                        "locator": {"connector": "test", "uri": "test://doc/1"},
                        "content_digest": version.content_digest,
                        "observed_at": version.observed_at.isoformat(),
                        "valid_at": version.valid_at.isoformat(),
                        "acl": {"allowed_principals": [str(pid)]},
                        "classification": "PUBLIC",
                        "state": "active",
                    }
                ],
            },
        )
        output = handler.execute(input)
        assert output.output_values["status"] == "completed"
        assert output.output_values["candidate_count"] == 1


class TestKnowledgeActivity:
    """Test Knowledge Activity: async boundary for knowledge retrieval."""

    @pytest.mark.asyncio
    async def test_activity_completes(self):
        pid = uuid4()
        org = uuid4()
        ws = uuid4()

        activity = KnowledgeActivity()
        input = KnowledgeActivityInput(
            run_id=str(uuid4()),
            task_id="t1",
            attempt_no=1,
            organization_id=str(org),
            workspace_id=str(ws),
            principal_id=str(pid),
            query={
                "query_id": "q1",
                "organization_id": str(org),
                "workspace_id": str(ws),
                "principal_id": str(pid),
                "text": "test",
                "sources": ["doc"],
                "top_k": 10,
            },
            acl={
                "principal_id": str(pid),
                "organization_id": str(org),
                "workspace_id": str(ws),
            },
            candidates=[],
        )
        output = await activity.execute(input)
        assert output.status == "completed"
        assert output.query_id == "q1"
        assert output.candidate_count == 0

    @pytest.mark.asyncio
    async def test_activity_with_valid_candidates(self):
        pid = uuid4()
        org = uuid4()
        ws = uuid4()
        version = _make_version(acl=ACLSnapshot(allowed_principals=(str(pid),)))

        activity = KnowledgeActivity()
        input = KnowledgeActivityInput(
            run_id=str(uuid4()),
            task_id="t1",
            attempt_no=1,
            organization_id=str(org),
            workspace_id=str(ws),
            principal_id=str(pid),
            query={
                "query_id": "q1",
                "organization_id": str(org),
                "workspace_id": str(ws),
                "principal_id": str(pid),
                "text": "test",
                "sources": ["doc"],
                "top_k": 10,
            },
            acl={
                "principal_id": str(pid),
                "organization_id": str(org),
                "workspace_id": str(ws),
            },
            candidates=[
                {
                    "id": str(version.id),
                    "source_object_id": str(version.source_object_id),
                    "version_seq": 1,
                    "locator": {"connector": "test", "uri": "test://doc/1"},
                    "content_digest": version.content_digest,
                    "observed_at": version.observed_at.isoformat(),
                    "valid_at": version.valid_at.isoformat(),
                    "acl": {"allowed_principals": [str(pid)]},
                    "classification": "PUBLIC",
                    "state": "active",
                }
            ],
        )
        output = await activity.execute(input)
        assert output.status == "completed"
        assert output.candidate_count == 1
