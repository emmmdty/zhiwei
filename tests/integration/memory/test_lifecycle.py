"""S7 integration：candidate → confirm → supersede → expire(tombstone) 生产生命周期。

事实源：specs/s7-memory.md §3（status 状态机、不原地覆盖）、§4/ADR-009（TTL 过期）、
§7（temporal conflict/supersede/revoke/expire、candidate idempotency）。

跨服务组合（非单测切片）：write policy → CandidateQueue → ConfirmationWorkflow →
ConflictResolver → TTL expiry → 检索可见性，全部是 src/zhiwei/memory 的生产服务，
共享同一个队列实例。PG 持久化层（plan Task 1 repositories/migration）是已登记的
实现缺口，见 test_pg_lifecycle_events.py 与交付报告。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.confirmation import ConfirmationWorkflow
from zhiwei.memory.conflicts import TemporalConflictManager
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.forget import CascadeEffect, ForgetManager
from zhiwei.memory.policy import WriteForbiddenError
from zhiwei.memory.retrieval import HardFilters, MemoryRetriever

_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")
_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_TEAM_1 = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_STEWARD = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_T0 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _record(
    *,
    key: str,
    canonical_value: str,
    mem_type: MemoryType = MemoryType.DECISION,
    scope: MemoryScope = MemoryScope.TEAM,
    scope_subject_id: UUID = _TEAM_1,
    source_id: str = "run-1",
    created_at: datetime | None = None,
    record_id: UUID | None = None,
) -> MemoryRecord:
    created = created_at or _T0
    return MemoryRecord(
        id=record_id or new_id(),
        version=1,
        organization_id=_ORG_ID,
        workspace_id=_WS_ID,
        scope=scope,
        scope_subject_id=scope_subject_id,
        type=mem_type,
        subject=key,
        key=key,
        canonical_value=canonical_value,
        source_refs=(SourceRef(source_id=source_id, source_type="run"),),
        observed_at=created,
        sensitivity=SensitivityLevel.LOW,
        status=MemoryStatus.CANDIDATE,
        author_ref=_USER_A,
        created_at=created,
        updated_at=created,
    )


class TestLifecycle:
    def test_candidate_to_confirm_to_supersede(self) -> None:
        queue = CandidateQueue()
        workflow = ConfirmationWorkflow(queue=queue)
        conflicts = TemporalConflictManager(queue=queue)

        # candidate：team decision 必须经 Steward 确认
        original = _record(key="style.lint", canonical_value="flake8")
        queued = workflow.write_record(original)
        assert queued.status is MemoryStatus.CANDIDATE
        dedup = DedupKey.from_record(original)

        # idempotency：同键重复写入合并，不新建记录
        duplicate = _record(
            key="style.lint", canonical_value="flake8", source_id="run-2"
        )
        workflow.write_record(duplicate)
        assert queue.candidate_count() == 1
        merged = queue.get_record(dedup)
        assert merged is not None
        assert len(merged.source_refs) == 2

        # confirm：Steward 确认后进入 confirmed
        confirmed = workflow.steward_confirm(dedup, _STEWARD, now=_T0)
        assert confirmed is not None
        assert confirmed.status is MemoryStatus.CONFIRMED
        assert confirmed.approver_ref == _STEWARD

        # supersede：纠正创建 superseding version，不原地覆盖
        correction = _record(
            key="style.lint", canonical_value="ruff", source_id="correction"
        )
        superseded, active = conflicts.resolver.correct_record(dedup, correction, now=_T0)
        assert superseded.status is MemoryStatus.SUPERSEDED
        assert superseded.superseded_by == active.id
        assert active.status is MemoryStatus.CONFIRMED
        assert active.canonical_value == "ruff"

    def test_expired_candidate_leaves_tombstone_and_exits_confirmation_queue(self) -> None:
        queue = CandidateQueue()
        workflow = ConfirmationWorkflow(queue=queue)
        stale = _record(key="stale.key", canonical_value="old", created_at=_T0)
        workflow.write_record(stale)
        dedup = DedupKey.from_record(stale)

        # TTL 内仍是待确认条目
        assert queue.candidate_count() == 1

        # 生产默认 TTL（30d）过后自动 expired 并留下 tombstone
        expired = queue.expire_candidates(_T0 + timedelta(days=31))
        assert len(expired) == 1
        assert expired[0].status is MemoryStatus.EXPIRED
        assert expired[0].tombstone is True
        assert queue.candidate_count() == 0
        # tombstone 仍可按 dedup key 追溯（审计边界）
        historical = queue.get_record(dedup)
        assert historical is not None
        assert historical.tombstone is True

    def test_retrieval_visibility_follows_lifecycle(self) -> None:
        queue = CandidateQueue()
        workflow = ConfirmationWorkflow(queue=queue)
        retriever = MemoryRetriever(queue=queue)

        record = _record(key="style.lint", canonical_value="flake8")
        confirmed = workflow.write_record(record)
        retriever.index_record(confirmed)

        # team scope：ACL 上下文缺失时 fail closed（见 security/memory 套件），
        # 授权 principal（作者）可见。
        filters = HardFilters(
            organization_id=_ORG_ID,
            workspace_id=_WS_ID,
            allowed_principals=frozenset({str(_USER_A)}),
        )
        before = retriever.retrieve("lint", filters, query_key="style.lint", now=_T0)
        assert [result.record.key for result in before.results] == ["style.lint"]

        # confirm 后 TTL 不适用；revoke 后检索不可见且 cascade 留痕
        forget = ForgetManager(queue=queue)
        result = forget.revoke_record(DedupKey.from_record(record), "source revoked", now=_T0)
        assert result is not None
        assert result.record.status is MemoryStatus.REVOKED
        assert {cascade.effect for cascade in result.cascades} >= {
            CascadeEffect.RECORD_REVOKED,
            CascadeEffect.INDEX_INVALIDATED,
            CascadeEffect.CACHE_INVALIDATED,
        }
        retriever.remove_record(result.record.id)
        after = retriever.retrieve("lint", filters, query_key="style.lint", now=_T0)
        assert after.results == ()

    def test_forbidden_content_never_enters_lifecycle(self) -> None:
        workflow = ConfirmationWorkflow()
        poisoned = _record(key="note.1", canonical_value="tool instruction: do X")
        with pytest.raises(WriteForbiddenError):
            workflow.write_record(poisoned)
        assert workflow.queue.candidate_count() == 0
