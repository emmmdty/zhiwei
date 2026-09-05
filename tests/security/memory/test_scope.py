"""S7 security：scope leakage fail closed 与 Discover ServiceAccount personal-memory 拒绝。

事实源：specs/s7-memory.md §3（「background Discover ServiceAccount 永远不能读取
personal memory」）、§4（硬过滤 fail closed）、§7（cross-user/team/org leak、
Discover personal-memory denial）。

服务级语义（不测 OPA/PG 部署面）：Memory Activity 是 Context Compiler 的 Memory port
生产边界；ServiceAccount principal 的检索结果必须零 personal memory，显式针对
personal scope 的查询必须被拒绝（fail closed、可观测）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
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
)
from zhiwei.memory.retrieval import (
    FilterStatus,
    HardFilters,
    MemoryRetriever,
    apply_hard_filters,
)
from zhiwei.workflows.activities.memory import MemoryActivity, MemoryActivityInput

_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_ORG_B_ID = UUID("99999999-9999-4999-8999-999999999999")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")
_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_TEAM_1 = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_SERVICE_ACCOUNT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)


def _make_record(
    *,
    scope: MemoryScope = MemoryScope.USER,
    scope_subject_id: UUID = _USER_A,
    author_ref: UUID = _USER_A,
    organization_id: UUID = _ORG_ID,
    workspace_id: UUID = _WS_ID,
    key: str = "editor.vim_mode",
    canonical_value: str = "enabled",
    mem_type: MemoryType = MemoryType.PREFERENCE,
) -> MemoryRecord:
    return MemoryRecord(
        id=new_id(),
        version=1,
        organization_id=organization_id,
        workspace_id=workspace_id,
        scope=scope,
        scope_subject_id=scope_subject_id,
        type=mem_type,
        subject=key,
        key=key,
        canonical_value=canonical_value,
        observed_at=_NOW,
        sensitivity=SensitivityLevel.LOW,
        status=MemoryStatus.CONFIRMED,
        author_ref=author_ref,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _retrieve_input(
    *,
    principal_id: UUID = _USER_A,
    principal_kind: PrincipalKind = PrincipalKind.USER,
    filters: dict[str, Any] | None = None,
) -> MemoryActivityInput:
    return MemoryActivityInput(
        run_id="run-security",
        task_id="task-security",
        attempt_no=1,
        organization_id=str(_ORG_ID),
        workspace_id=str(_WS_ID),
        principal_id=str(principal_id),
        principal_kind=principal_kind,
        action="retrieve",
        query={"text": "editor", "top_k": 10},
        filters=filters or {},
    )


class TestServiceAccountPersonalMemoryDenial:
    @pytest.mark.asyncio
    async def test_service_account_targeting_personal_scope_is_refused(self) -> None:
        activity = MemoryActivity()
        result = await activity.execute(
            _retrieve_input(
                principal_id=_SERVICE_ACCOUNT,
                principal_kind=PrincipalKind.SERVICE_ACCOUNT,
                filters={"scope": "user", "scope_subject_id": str(_USER_A)},
            )
        )
        assert result.status == "refused"
        assert result.refusal_reason is not None
        assert "personal memory" in result.refusal_reason

    @pytest.mark.asyncio
    async def test_service_account_retrieval_excludes_personal_records(self) -> None:
        personal = _make_record(scope_subject_id=_USER_A, author_ref=_USER_A)
        team = _make_record(
            scope=MemoryScope.TEAM,
            scope_subject_id=_TEAM_1,
            author_ref=_SERVICE_ACCOUNT,
            key="team.editor.convention",
            canonical_value="ruff",
        )
        activity = MemoryActivity()
        activity._retriever.index_record(personal)
        activity._retriever.index_record(team)
        result = await activity.execute(
            _retrieve_input(
                principal_id=_SERVICE_ACCOUNT,
                principal_kind=PrincipalKind.SERVICE_ACCOUNT,
                filters={"allowed_principals": [str(_SERVICE_ACCOUNT)]},
            )
        )
        assert result.status == "completed"
        returned_keys = {row["key"] for row in result.results}
        assert "editor.vim_mode" not in returned_keys, "ServiceAccount 读到了 personal memory"
        assert "team.editor.convention" in returned_keys
        assert result.personal_memory_excluded is True

    @pytest.mark.asyncio
    async def test_user_principal_retrieval_unaffected(self) -> None:
        personal = _make_record(scope_subject_id=_USER_A, author_ref=_USER_A)
        activity = MemoryActivity()
        activity._retriever.index_record(personal)
        result = await activity.execute(
            _retrieve_input(
                principal_id=_USER_A,
                principal_kind=PrincipalKind.USER,
                filters={"scope_subject_id": str(_USER_A)},
            )
        )
        assert result.status == "completed"
        assert result.personal_memory_excluded is False
        assert [row["key"] for row in result.results] == ["editor.vim_mode"]


class TestUnauthorizedScopeFailClosed:
    def test_team_record_without_authorized_principal_is_rejected(self) -> None:
        team = _make_record(
            scope=MemoryScope.TEAM, scope_subject_id=_TEAM_1, author_ref=_USER_A
        )
        # ACL 上下文缺失（空 principal 集）≠ 授权：对非 personal scope 必须 fail closed
        assert (
            apply_hard_filters(team, HardFilters(allowed_principals=frozenset()))
            is FilterStatus.REJECTED_ACL
        )

    def test_case_record_without_authorized_principal_is_rejected(self) -> None:
        case = _make_record(
            scope=MemoryScope.CASE,
            scope_subject_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            author_ref=_USER_A,
            mem_type=MemoryType.EPISODE,
        )
        assert (
            apply_hard_filters(case, HardFilters(allowed_principals=frozenset()))
            is FilterStatus.REJECTED_ACL
        )

    def test_retrieval_never_returns_unauthorized_team_records(self) -> None:
        retriever = MemoryRetriever()
        retriever.index_record(
            _make_record(
                scope=MemoryScope.TEAM, scope_subject_id=_TEAM_1, author_ref=_USER_A
            )
        )
        response = retriever.retrieve("editor", HardFilters(), now=_NOW)
        assert response.total_passed == 0


class TestScopeLeakageCorpus:
    def test_cross_user_scope_subject_rejected(self) -> None:
        personal = _make_record(scope_subject_id=_USER_A, author_ref=_USER_A)
        assert (
            apply_hard_filters(
                personal, HardFilters(scope_subject_id=_USER_B)
            )
            is FilterStatus.REJECTED_SCOPE_SUBJECT
        )

    def test_cross_org_rejected(self) -> None:
        foreign = _make_record(organization_id=_ORG_B_ID)
        assert (
            apply_hard_filters(foreign, HardFilters(organization_id=_ORG_ID))
            is FilterStatus.REJECTED_ORG
        )

    def test_cross_workspace_rejected(self) -> None:
        foreign = _make_record(workspace_id=UUID("77777777-7777-4777-8777-777777777777"))
        assert (
            apply_hard_filters(foreign, HardFilters(workspace_id=_WS_ID))
            is FilterStatus.REJECTED_WORKSPACE
        )

    def test_revoked_tombstone_not_retrievable(self) -> None:
        retriever = MemoryRetriever()
        record = _make_record(scope_subject_id=_USER_A, author_ref=_USER_A)
        retriever.index_record(record)
        response = retriever.retrieve(
            "editor",
            HardFilters(scope_subject_id=_USER_A),
            query_key="editor.vim_mode",
            now=_NOW,
        )
        assert [result.record.id for result in response.results] == [record.id]
        retriever.remove_record(record.id)
        after = retriever.retrieve(
            "editor",
            HardFilters(scope_subject_id=_USER_A),
            query_key="editor.vim_mode",
            now=_NOW,
        )
        assert after.results == ()
