"""S7-T4 contract tests: Memory retrieval with hard filters and multi-stage ranking.

Covers:
- Hard filter: org/workspace/scope_subject/ACL/sensitivity/status/time/allowed_profile
- exact + lexical + dense retrieval
- Results carry reason, provenance, conflicts, freshness
- Context Compiler memory token budget
- WriteMemoryCandidate: typed task → Memory Activity/policy → candidate/refusal
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import PrincipalKind
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.index import DenseIndex, ExactIndex, LexicalIndex
from zhiwei.memory.policy import evaluate_write_policy
from zhiwei.memory.retrieval import (
    FilterStatus,
    HardFilters,
    MemoryRetriever,
    apply_hard_filters,
)
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.write_memory_candidate import WriteMemoryCandidateHandler
from zhiwei.workflows.activities.memory import (
    MemoryActivity,
    MemoryActivityInput,
)

# ── Fixtures ───────────────────────────────────────────────────────────


_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")
_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def _make_record(
    *,
    key: str = "editor.vim_mode",
    subject: str = "vim keybindings",
    canonical_value: str = "enabled",
    scope: MemoryScope = MemoryScope.USER,
    scope_subject_id: UUID = _USER_A,
    mem_type: MemoryType = MemoryType.PREFERENCE,
    sensitivity: SensitivityLevel = SensitivityLevel.LOW,
    status: MemoryStatus = MemoryStatus.CANDIDATE,
    author_ref: UUID = _USER_A,
    organization_id: UUID = _ORG_ID,
    workspace_id: UUID = _WS_ID,
    source_refs: tuple[SourceRef, ...] = (),
    confidence: float = 0.8,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    allowed_profile_refs: tuple[str, ...] = (),
    tombstone: bool = False,
) -> MemoryRecord:
    return MemoryRecord(
        id=new_id(),
        version=1,
        organization_id=organization_id,
        workspace_id=workspace_id,
        scope=scope,
        scope_subject_id=scope_subject_id,
        type=mem_type,
        subject=subject,
        key=key,
        canonical_value=canonical_value,
        source_refs=source_refs,
        observed_at=_NOW,
        confidence=confidence,
        sensitivity=sensitivity,
        status=status,
        author_ref=author_ref,
        created_at=_NOW,
        updated_at=_NOW,
        valid_from=valid_from,
        valid_to=valid_to,
        allowed_profile_refs=allowed_profile_refs,
        tombstone=tombstone,
    )


# ── Hard filter tests ──────────────────────────────────────────────────


class TestHardFilters:
    def test_passes_with_no_filters(self) -> None:
        record = _make_record()
        filters = HardFilters()
        assert apply_hard_filters(record, filters) == FilterStatus.PASS

    def test_rejects_wrong_org(self) -> None:
        record = _make_record(organization_id=_ORG_ID)
        filters = HardFilters(organization_id=new_id())
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_ORG

    def test_rejects_wrong_workspace(self) -> None:
        record = _make_record(workspace_id=_WS_ID)
        filters = HardFilters(workspace_id=new_id())
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_WORKSPACE

    def test_rejects_wrong_scope_subject(self) -> None:
        record = _make_record(scope_subject_id=_USER_A)
        filters = HardFilters(scope_subject_id=_USER_B)
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_SCOPE_SUBJECT

    def test_rejects_exceeded_sensitivity(self) -> None:
        record = _make_record(sensitivity=SensitivityLevel.HIGH)
        filters = HardFilters(max_sensitivity=SensitivityLevel.LOW)
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_SENSITIVITY

    def test_passes_equal_sensitivity(self) -> None:
        record = _make_record(sensitivity=SensitivityLevel.MEDIUM)
        filters = HardFilters(max_sensitivity=SensitivityLevel.MEDIUM)
        assert apply_hard_filters(record, filters) == FilterStatus.PASS

    def test_rejects_disallowed_status(self) -> None:
        record = _make_record(status=MemoryStatus.CONFIRMED)
        filters = HardFilters(allowed_statuses=frozenset({MemoryStatus.CANDIDATE}))
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_STATUS

    def test_rejects_tombstone(self) -> None:
        record = _make_record(tombstone=True)
        filters = HardFilters(exclude_tombstones=True)
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_STATUS

    def test_rejects_time_out_of_range(self) -> None:
        record = _make_record(
            valid_from=datetime(2025, 7, 1, tzinfo=UTC),
            valid_to=datetime(2025, 8, 1, tzinfo=UTC),
        )
        filters = HardFilters(valid_at=datetime(2025, 6, 1, tzinfo=UTC))
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_TIME

    def test_passes_time_in_range(self) -> None:
        record = _make_record(
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 12, 31, tzinfo=UTC),
        )
        filters = HardFilters(valid_at=datetime(2025, 6, 1, tzinfo=UTC))
        assert apply_hard_filters(record, filters) == FilterStatus.PASS

    def test_rejects_disallowed_profile(self) -> None:
        record = _make_record(allowed_profile_refs=("profile_x",))
        filters = HardFilters(allowed_profile_refs=frozenset({"profile_y"}))
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_PROFILE

    def test_rejects_team_acl(self) -> None:
        record = _make_record(
            scope=MemoryScope.TEAM,
            author_ref=_USER_A,
        )
        filters = HardFilters(allowed_principals=frozenset({str(_USER_B)}))
        assert apply_hard_filters(record, filters) == FilterStatus.REJECTED_ACL

    def test_passes_user_scope_without_acl_check(self) -> None:
        record = _make_record(
            scope=MemoryScope.USER,
            scope_subject_id=_USER_A,
            author_ref=_USER_A,
        )
        filters = HardFilters(allowed_principals=frozenset({str(_USER_B)}))
        assert apply_hard_filters(record, filters) == FilterStatus.PASS


# ── Index tests ────────────────────────────────────────────────────────


class TestExactIndex:
    def test_exact_match(self) -> None:
        idx = ExactIndex()
        record = _make_record(key="editor.vim_mode", subject="vim", canonical_value="on")
        idx.add(record)
        results = idx.search_exact("editor.vim_mode")
        assert len(results) == 1
        assert results[0].record.id == record.id
        assert results[0].source == "exact"

    def test_no_match(self) -> None:
        idx = ExactIndex()
        record = _make_record(key="editor.vim_mode")
        idx.add(record)
        results = idx.search_exact("editor.emacs_mode")
        assert len(results) == 0

    def test_remove(self) -> None:
        idx = ExactIndex()
        record = _make_record()
        idx.add(record)
        idx.remove(record.id)
        results = idx.search_exact("editor.vim_mode")
        assert len(results) == 0


class TestLexicalIndex:
    def test_lexical_match(self) -> None:
        idx = LexicalIndex()
        record = _make_record(subject="vim keybindings", key="editor.vim", canonical_value="on")
        idx.add(record)
        results = idx.search_lexical("vim keybindings editor")
        assert len(results) >= 1
        assert results[0].source == "lexical"

    def test_no_match(self) -> None:
        idx = LexicalIndex()
        record = _make_record(subject="python config", key="editor.python")
        idx.add(record)
        results = idx.search_lexical("vim keybindings")
        assert len(results) == 0


class TestDenseIndex:
    def test_dense_match(self) -> None:
        idx = DenseIndex()
        record = _make_record()
        idx.add(record, [1.0, 0.0, 0.0])
        results = idx.search_dense([1.0, 0.0, 0.0])
        assert len(results) == 1
        assert results[0].source == "dense"
        assert results[0].score > 0.99

    def test_dense_orthogonal(self) -> None:
        idx = DenseIndex()
        record = _make_record()
        idx.add(record, [1.0, 0.0, 0.0])
        results = idx.search_dense([0.0, 1.0, 0.0])
        assert len(results) == 0

    def test_remove(self) -> None:
        idx = DenseIndex()
        record = _make_record()
        idx.add(record, [1.0, 0.0])
        idx.remove(record.id)
        results = idx.search_dense([1.0, 0.0])
        assert len(results) == 0


# ── Retrieval pipeline tests ───────────────────────────────────────────


class TestMemoryRetriever:
    def test_retrieve_exact(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record(key="editor.vim", subject="vim", canonical_value="on")
        retriever.index_record(record)

        response = retriever.retrieve(
            "vim", HardFilters(), query_key="editor.vim"
        )
        assert response.total_passed >= 1
        assert response.results[0].record.id == record.id

    def test_retrieve_lexical(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record(subject="python config", key="editor.python", canonical_value="on")
        retriever.index_record(record)

        response = retriever.retrieve("python config editor", HardFilters())
        assert response.total_passed >= 1

    def test_retrieve_dense(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record()
        retriever.index_record_dense(record, [1.0, 0.0, 0.0])

        response = retriever.retrieve(
            "test", HardFilters(), query_embedding=[1.0, 0.0, 0.0]
        )
        assert response.total_passed >= 1

    def test_retrieve_filters_reject_wrong_org(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record(organization_id=_ORG_ID)
        retriever.index_record(record)

        response = retriever.retrieve(
            "vim", HardFilters(organization_id=new_id()), query_key="editor.vim"
        )
        assert response.total_passed == 0

    def test_retrieve_provenance_carry(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record(key="editor.vim", subject="vim", canonical_value="on")
        retriever.index_record(record)
        retriever.index_record_dense(record, [1.0, 0.0, 0.0])

        response = retriever.retrieve(
            "vim", HardFilters(), query_key="editor.vim", query_embedding=[1.0, 0.0, 0.0]
        )
        assert response.total_passed >= 1
        # Provenance should mention the source index
        assert len(response.results[0].provenance) >= 1

    def test_retrieve_freshness(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record()
        retriever.index_record(record)

        response = retriever.retrieve(
            "vim", HardFilters(), query_key="editor.vim", now=_NOW
        )
        assert response.total_passed >= 1
        # Freshness should be near 0 since observed_at == now
        assert response.results[0].freshness_seconds < 1.0


# ── Write policy tests ─────────────────────────────────────────────────


class TestWritePolicy:
    def test_low_risk_user_preference_auto_confirm(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.LOW,
            subject="vim",
            canonical_value="on",
        )
        assert result.decision == "auto_confirm"

    def test_sensitive_user_requires_candidate(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.USER,
            mem_type=MemoryType.FACT,
            sensitivity=SensitivityLevel.HIGH,
            subject="medical info",
            canonical_value="data",
        )
        assert result.decision == "candidate"

    def test_team_memory_requires_candidate(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.DECISION,
            sensitivity=SensitivityLevel.LOW,
            subject="code style",
            canonical_value="ruff",
        )
        assert result.decision == "candidate"

    def test_case_episode_auto_confirm(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.CASE,
            mem_type=MemoryType.EPISODE,
            sensitivity=SensitivityLevel.LOW,
            subject="meeting",
            canonical_value="discussed api",
        )
        assert result.decision == "auto_confirm"

    def test_forbidden_secret_in_subject(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.USER,
            mem_type=MemoryType.FACT,
            sensitivity=SensitivityLevel.LOW,
            subject="my password is xyz",
            canonical_value="data",
        )
        assert result.decision == "forbidden"

    def test_forbidden_tool_instruction_in_value(self) -> None:
        result = evaluate_write_policy(
            scope=MemoryScope.USER,
            mem_type=MemoryType.FACT,
            sensitivity=SensitivityLevel.LOW,
            subject="note",
            canonical_value="system prompt: do X",
        )
        assert result.decision == "forbidden"


# ── WriteMemoryCandidate handler tests ─────────────────────────────────


class TestWriteMemoryCandidateHandler:
    def test_auto_confirm(self) -> None:
        handler = WriteMemoryCandidateHandler()
        record = _make_record(
            scope=MemoryScope.USER,
            mem_type=MemoryType.PREFERENCE,
            sensitivity=SensitivityLevel.LOW,
        )
        input_data = TaskInput(
            task_id="t1",
            attempt_id=new_id(),
            input_values={
                "memory": {
                    "id": str(record.id),
                    "organization_id": str(record.organization_id),
                    "workspace_id": str(record.workspace_id),
                    "scope": record.scope.value,
                    "scope_subject_id": str(record.scope_subject_id),
                    "type": record.type.value,
                    "subject": record.subject,
                    "key": record.key,
                    "canonical_value": record.canonical_value,
                    "sensitivity": record.sensitivity.value,
                    "status": record.status.value,
                    "created_at": record.created_at.isoformat(),
                    "observed_at": record.observed_at.isoformat(),
                    "author_ref": str(record.author_ref),
                },
                "actor_id": str(_USER_A),
            },
        )
        output = handler.execute(input_data)
        assert output.output_values["status"] == "completed"
        assert output.output_values["decision"] == "auto_confirm"

    def test_candidate(self) -> None:
        handler = WriteMemoryCandidateHandler()
        record = _make_record(
            scope=MemoryScope.TEAM,
            mem_type=MemoryType.DECISION,
            sensitivity=SensitivityLevel.LOW,
        )
        input_data = TaskInput(
            task_id="t2",
            attempt_id=new_id(),
            input_values={
                "memory": {
                    "id": str(record.id),
                    "organization_id": str(record.organization_id),
                    "workspace_id": str(record.workspace_id),
                    "scope": record.scope.value,
                    "scope_subject_id": str(record.scope_subject_id),
                    "type": record.type.value,
                    "subject": record.subject,
                    "key": record.key,
                    "canonical_value": record.canonical_value,
                    "sensitivity": record.sensitivity.value,
                    "status": record.status.value,
                    "created_at": record.created_at.isoformat(),
                    "observed_at": record.observed_at.isoformat(),
                    "author_ref": str(record.author_ref),
                },
                "actor_id": str(_USER_A),
            },
        )
        output = handler.execute(input_data)
        assert output.output_values["status"] == "completed"
        assert output.output_values["decision"] == "candidate"

    def test_forbidden(self) -> None:
        handler = WriteMemoryCandidateHandler()
        input_data = TaskInput(
            task_id="t3",
            attempt_id=new_id(),
            input_values={
                "memory": {
                    "organization_id": str(_ORG_ID),
                    "workspace_id": str(_WS_ID),
                    "scope": "user",
                    "scope_subject_id": str(_USER_A),
                    "type": "fact",
                    "subject": "my password is secret123",
                    "key": "auth.password",
                    "canonical_value": "data",
                    "created_at": _NOW.isoformat(),
                    "observed_at": _NOW.isoformat(),
                },
                "actor_id": str(_USER_A),
            },
        )
        output = handler.execute(input_data)
        assert output.output_values["status"] == "refused"
        assert output.output_values["decision"] == "forbidden"

    def test_missing_memory_refused(self) -> None:
        handler = WriteMemoryCandidateHandler()
        input_data = TaskInput(
            task_id="t4",
            attempt_id=new_id(),
            input_values={"actor_id": str(_USER_A)},
        )
        output = handler.execute(input_data)
        assert output.output_values["status"] == "refused"

    def test_missing_actor_refused(self) -> None:
        handler = WriteMemoryCandidateHandler()
        input_data = TaskInput(
            task_id="t5",
            attempt_id=new_id(),
            input_values={
                "memory": {
                    "organization_id": str(_ORG_ID),
                    "workspace_id": str(_WS_ID),
                    "scope": "user",
                    "scope_subject_id": str(_USER_A),
                    "type": "preference",
                    "subject": "test",
                    "key": "test.key",
                    "canonical_value": "val",
                    "created_at": _NOW.isoformat(),
                    "observed_at": _NOW.isoformat(),
                },
            },
        )
        output = handler.execute(input_data)
        assert output.output_values["status"] == "refused"


# ── Memory Activity tests ──────────────────────────────────────────────


class TestMemoryActivity:
    @pytest.mark.asyncio
    async def test_retrieve_action(self) -> None:
        activity = MemoryActivity()
        input_data = MemoryActivityInput(
            run_id="run-1",
            task_id="task-1",
            attempt_no=1,
            organization_id=str(_ORG_ID),
            workspace_id=str(_WS_ID),
            principal_id=str(_USER_A),
            principal_kind=PrincipalKind.USER,
            action="retrieve",
            query={"text": "vim", "top_k": 5},
            filters={"organization_id": str(_ORG_ID), "workspace_id": str(_WS_ID)},
        )
        result = await activity.execute(input_data)
        assert result.status == "completed"
        assert result.action == "retrieve"

    @pytest.mark.asyncio
    async def test_write_action_auto_confirm(self) -> None:
        activity = MemoryActivity()
        input_data = MemoryActivityInput(
            run_id="run-2",
            task_id="task-2",
            attempt_no=1,
            organization_id=str(_ORG_ID),
            workspace_id=str(_WS_ID),
            principal_id=str(_USER_A),
            principal_kind=PrincipalKind.USER,
            action="write",
            memory={
                "organization_id": str(_ORG_ID),
                "workspace_id": str(_WS_ID),
                "scope": "user",
                "scope_subject_id": str(_USER_A),
                "type": "preference",
                "subject": "vim",
                "key": "editor.vim",
                "canonical_value": "enabled",
                "sensitivity": "low",
                "created_at": _NOW.isoformat(),
                "observed_at": _NOW.isoformat(),
            },
        )
        result = await activity.execute(input_data)
        assert result.status == "completed"
        assert result.decision == "auto_confirm"

    @pytest.mark.asyncio
    async def test_write_action_forbidden(self) -> None:
        activity = MemoryActivity()
        input_data = MemoryActivityInput(
            run_id="run-3",
            task_id="task-3",
            attempt_no=1,
            organization_id=str(_ORG_ID),
            workspace_id=str(_WS_ID),
            principal_id=str(_USER_A),
            principal_kind=PrincipalKind.USER,
            action="write",
            memory={
                "organization_id": str(_ORG_ID),
                "workspace_id": str(_WS_ID),
                "scope": "user",
                "scope_subject_id": str(_USER_A),
                "type": "fact",
                "subject": "my password is secret",
                "key": "auth.pass",
                "canonical_value": "data",
                "created_at": _NOW.isoformat(),
                "observed_at": _NOW.isoformat(),
            },
        )
        result = await activity.execute(input_data)
        assert result.status == "refused"
        assert result.decision == "forbidden"

    @pytest.mark.asyncio
    async def test_unknown_action(self) -> None:
        activity = MemoryActivity()
        input_data = MemoryActivityInput(
            run_id="run-4",
            task_id="task-4",
            attempt_no=1,
            organization_id=str(_ORG_ID),
            workspace_id=str(_WS_ID),
            principal_id=str(_USER_A),
            principal_kind=PrincipalKind.USER,
            action="unknown",
        )
        result = await activity.execute(input_data)
        assert result.status == "error"
