"""S7-T6 Memory Center API — user views, confirm/correct/resolve/revoke/delete/export.

用户查看本人和可见团队/Case memory，按来源/类型/状态筛选，执行
confirm/correct/resolve/revoke/delete/export。P2b 重接（F-R6-02/03、F-R6-12、
F-R3-09）：进程内单例 store 移除，全部端点落 PgMemoryRepository（0012
memory_records + lifecycle 台账，同事务）；读路径 PEP（team_memory.read_
authorized）、mutation PEP（confirm/correct/conflict/revoke cell）+ 同事务
allowed 审计 + denied/failed 独立事务审计；revoke/delete 经 ForgetManager
语义产生 CascadeEvent 并落台账与审计链。

角色语义由冻结矩阵 cell（Rego 唯一事实）裁决：member 不再能自确认/纠正/
删除（stub 期无角色门，行为收紧登记于 R6 台账）；delete 与 revoke 对齐
终态检查；conflict 解析 fail closed——DATA_MODEL 无冲突持久实体（记录
唯一携带 conflict_refs），解析走 correct（supersede）路径，stub 期由进程内
临时状态支撑的 200-resolved 不再可复现。

事实源：S7 spec §4/§5（Memory Center）、ADR-006/009、
docs/PERMISSIONS.md §3.1 行 10、findings F-R6-02/03/12、F-R3-09。
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.identity.domain import ActorContext
from zhiwei.memory.candidates import DedupKey
from zhiwei.memory.conflicts import ConflictDetector, ConflictRecord
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from zhiwei.memory.events import (
    ACTION_RECORD_CACHE_INVALIDATED,
    ACTION_RECORD_INDEX_INVALIDATED,
    MemoryLifecycleLedger,
)
from zhiwei.memory.forget import CascadeEffect, build_cascade_events
from zhiwei.memory.repositories import PgMemoryRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

logger = logging.getLogger(__name__)

_AUDIT_RESOURCE_TYPE = "memory_record"

_AUDIT_ACTION_CONFIRM = "memory.record.confirm"
_AUDIT_ACTION_CORRECT = "memory.record.correct"
_AUDIT_ACTION_REVOKE = "memory.record.revoke"
_AUDIT_ACTION_DELETE = "memory.record.delete"
_AUDIT_ACTION_RESOLVE = "memory.conflict.resolve"

_CASCADE_LEDGER_ACTION = {
    CascadeEffect.INDEX_INVALIDATED: ACTION_RECORD_INDEX_INVALIDATED,
    CascadeEffect.CACHE_INVALIDATED: ACTION_RECORD_CACHE_INVALIDATED,
}

# conflict 派生投影的检测面：仅活跃记录（终态/superseded 历史不是未解决冲突）
_CONFLICT_ACTIVE_STATUSES = (MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED)


class MemoryTransitionRejected(RuntimeError):
    """业务拒绝（PEP 放行后）：终态/状态前置不满足——failed 审计 reason 码映射。"""

    def __init__(self, status_value: MemoryStatus | None) -> None:
        self.status_value = status_value
        super().__init__(
            "memory transition rejected"
            if status_value is None
            else f"memory transition rejected: {status_value.value}"
        )


# ── API response / request models ──────────────────────────────────────


class MemoryRecordResponse(BaseModel):
    """Memory record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    version: int
    organization_id: UUID
    workspace_id: UUID
    scope: str
    scope_subject_id: UUID
    type: str
    subject: str
    key: str
    canonical_value: str
    source_refs: list[dict[str, Any]]
    observed_at: str
    confidence: float
    sensitivity: str
    status: str
    author_ref: UUID
    approver_ref: UUID | None
    conflict_refs: list[str]
    created_at: str
    updated_at: str
    tombstone: bool = False


class ConflictResponse(BaseModel):
    """Conflict record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: UUID
    kind: str
    record_a_id: UUID
    record_b_id: UUID
    detected_at: str
    resolved: bool
    resolved_by: UUID | None
    resolved_at: str | None


class ConfirmRequest(BaseModel):
    """Request body for confirming a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID


class CorrectRequest(BaseModel):
    """Request body for correcting (superseding) a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    canonical_value: str
    subject: str | None = None


class ResolveRequest(BaseModel):
    """Request body for resolving a conflict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: UUID


class RevokeRequest(BaseModel):
    """Request body for revoking a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    reason: str = ""


class DeleteRequest(BaseModel):
    """Request body for deleting a memory record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID


class ExportRequest(BaseModel):
    """Request body for exporting memory records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str | None = None
    record_type: str | None = None
    status: str | None = None


class ExportResponse(BaseModel):
    """Export response with records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[MemoryRecordResponse]
    count: int


class MemoryStatsResponse(BaseModel):
    """Memory center statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_records: int
    by_status: dict[str, int]
    by_scope: dict[str, int]
    by_type: dict[str, int]
    unresolved_conflicts: int


# ── conflict 派生投影（S7 §4：未解决冲突同时投影）────────────────────────


def _derive_conflicts(records: list[MemoryRecord]) -> list[ConflictRecord]:
    """从租户**活跃**记录派生未解决冲突（确定性 id，无进程内状态）。

    同键（dedup_hash）不同值即 value conflict；检测面只取 candidate/confirmed
    ——superseded 历史、tombstone（revoked/expired）不是「未解决冲突」（S7 §4），
    终态后同键重写也是 ADR-009 允许的正常流；supersede 相邻对再排除一层
    （纠正对原/新版本并存是解决语义本身）。subject 冲突按构造不可能（subject
    参与 dedup 键），不重复检测。
    """
    detector = ConflictDetector()
    by_hash: dict[str, list[MemoryRecord]] = {}
    for record in records:
        if record.status not in _CONFLICT_ACTIVE_STATUSES:
            continue
        by_hash.setdefault(record.dedup_hash, []).append(record)
    for group in by_hash.values():
        for a, b in combinations(group, 2):
            if a.superseded_by == b.id or b.superseded_by == a.id:
                continue
            detector.detect_value_conflict(a, b)
            detector.detect_temporal_conflict(a, b)
    conflicts = [
        replace(
            conflict,
            conflict_id=uuid5(
                NAMESPACE_URL,
                f"memory-conflict:{conflict.record_a_id}:{conflict.record_b_id}:"
                f"{conflict.kind.value}",
            ),
        )
        for conflict in detector.get_unresolved_conflicts()
    ]
    conflicts.sort(key=lambda c: c.conflict_id)
    return conflicts


# ── Router factory ─────────────────────────────────────────────────────


def create_memory_router(
    *,
    # Depends 消费面接受 FastAPI 依赖的两种合法形状（零参或 request 形参）——
    # 生产组合根传入 create_session_actor_dependency（request 形参，auth.py 冻结
    # 契约）；窄化成 () 形状曾把裸调用误标为合法（S11 followup-2 实测缺陷）
    actor_dependency: Callable[..., ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Create the memory center API router.

    P2b 重接：sessions/policy_enforcer 必须由组合根提供（fail closed，缺失
    在构造期拒绝——客户端声明不是授权事实）。
    """
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    if sessions is None:
        raise TypeError("sessions must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    def _tenant(actor: ActorContext) -> TenantContext:
        if actor.organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        if actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="workspace context required",
            )
        return TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )

    async def _authorize_read(
        request_scope: Request,
        actor: ActorContext,
        context: TenantContext,
        resource_id: UUID,
    ) -> None:
        """读路径 PEP：team_memory.read_authorized cell（不写审计）。

        actor 必须来自端点的 Depends(actor_dependency)（生产依赖签名是
        async + request 形参，auth.py 冻结契约）——在此裸调用
        actor_dependency() 会 TypeError 500（S11 followup-2 任务三实测
        发现；零参 stub 测试曾掩盖该接线缺陷）。
        """
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.READ_AUTHORIZED,
            resource_id=resource_id,
            trace_id=trace_id,
        )

    def _visible_to(record: MemoryRecord, actor: ActorContext) -> bool:
        """F-R6-12：单记录读与列表同可见性——他人 personal 记录不可按 ID
        枚举（与不存在同形 404，防存在性泄漏）。"""
        return not (
            record.scope == MemoryScope.USER
            and record.scope_subject_id != actor.principal_id
        )

    @router.get("/records", response_model=list[MemoryRecordResponse])
    async def list_memory_records(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        scope: str | None = Query(None),
        type: str | None = Query(None, alias="type"),
        status_filter: str | None = Query(None, alias="status"),
        source: str | None = Query(None),
    ) -> list[MemoryRecordResponse]:
        context = _tenant(actor)
        await _authorize_read(request_scope, actor, context, context.organization_id)
        scope_enum = MemoryScope(scope) if scope else None
        type_enum = MemoryType(type) if type else None
        status_enum = MemoryStatus(status_filter) if status_filter else None
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(session, context)
            records = await repo.list_for_user(
                actor.principal_id,
                scope=scope_enum,
                mem_type=type_enum,
                mem_status=status_enum,
            )
        if source is not None:
            records = [
                r for r in records if any(sr.source_type == source for sr in r.source_refs)
            ]
        return [_to_response(r) for r in records]

    @router.get("/records/{record_id}", response_model=MemoryRecordResponse)
    async def get_memory_record(
        request_scope: Request,
        record_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        context = _tenant(actor)
        await _authorize_read(request_scope, actor, context, record_id)
        async with tenant_session(sessions, context) as session:
            record = await PgMemoryRepository(session, context).get_by_id(record_id)
        if record is None or not _visible_to(record, actor):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory record not found",
            )
        return _to_response(record)

    @router.post("/records/{record_id}/confirm", response_model=MemoryRecordResponse)
    async def confirm_record(
        request_scope: Request,
        record_id: UUID,
        request: ConfirmRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        # PEP 先于业务事务（cell 语义由 Rego 裁决：memory_steward）；denied →
        # 独立事务 denied 审计 + 403。
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_CONFIRM,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.CONFIRM,
            resource_id=record_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(
                session, context, ledger=MemoryLifecycleLedger(session, context)
            )
            record = await repo.get_by_id(record_id)
            if record is None:
                # 与不存在同形 404（租户作用域查询）；无可变更对象不写 allowed/failed 审计
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="memory record not found",
                )
            if record.status is not MemoryStatus.CANDIDATE:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_CONFIRM,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=record_id,
                    error=MemoryTransitionRejected(record.status),
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record status is {record.status.value}, expected candidate",
                )
            confirmed = await repo.confirm_candidate(
                _dedup_key(record), actor.principal_id
            )
            if confirmed is None:  # pragma: no cover - 状态已在上面的 CAS 前置校验
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record status is {record.status.value}, expected candidate",
                )
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CONFIRM,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=record_id,
                resource_version=confirmed.version,
                authorization=authorization,
            )
        return _to_response(confirmed)

    @router.post("/records/{record_id}/correct", response_model=MemoryRecordResponse)
    async def correct_record(
        request_scope: Request,
        record_id: UUID,
        request: CorrectRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_CORRECT,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.CORRECT,
            resource_id=record_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(
                session, context, ledger=MemoryLifecycleLedger(session, context)
            )
            original = await repo.get_by_id(record_id)
            if original is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="memory record not found",
                )
            now = ensure_utc(datetime.now(tz=UTC))
            # (status='superseded') = (superseded_by IS NOT NULL) 约束：confirmed
            # 新版本不携带 superseded_by；原记录由域层 supersede 语义回填。
            corrected = original.model_copy(
                update={
                    "id": new_id(),
                    "version": original.version + 1,
                    "canonical_value": request.canonical_value,
                    "subject": request.subject or original.subject,
                    "status": MemoryStatus.CONFIRMED,
                    "approver_ref": actor.principal_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            try:
                _, confirmed = await repo.supersede_record(
                    _dedup_key(original), corrected
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="memory record not found",
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_CORRECT,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=record_id,
                resource_version=confirmed.version,
                authorization=authorization,
            )
        return _to_response(confirmed)

    @router.post("/records/{record_id}/revoke", response_model=MemoryRecordResponse)
    async def revoke_record(
        request_scope: Request,
        record_id: UUID,
        request: RevokeRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryRecordResponse:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_REVOKE,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.REVOKE,
            resource_id=record_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = MemoryLifecycleLedger(session, context)
            repo = PgMemoryRepository(session, context, ledger=ledger)
            record = await repo.get_by_id(record_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="memory record not found",
                )
            if record.terminal_status():
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_REVOKE,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=record_id,
                    error=MemoryTransitionRejected(record.status),
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record is already in terminal status: {record.status.value}",
                )
            # ForgetManager 语义级联（F-R6-02）：级联事件与转移同事务落台账 +
            # 审计链；record_revoked 由仓储转移本身落账，此处补 index/cache。
            actor_ref = f"user:{actor.principal_id}"
            cascades = build_cascade_events(record, request.reason)
            revoked = await repo.revoke_record(
                _dedup_key(record),
                request.reason,
                actor_ref=actor_ref,
            )
            if revoked is None:  # pragma: no cover - 终态已在上面的前置校验拦截
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record is already in terminal status: {record.status.value}",
                )
            for event in cascades:
                action = _CASCADE_LEDGER_ACTION.get(event.effect)
                if action is None:
                    continue
                await ledger.record_transition(
                    revoked,
                    action=action,
                    from_status=revoked.status.value,
                    to_status=revoked.status.value,
                    actor_ref=actor_ref,
                    reason=request.reason,
                )
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_REVOKE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=record_id,
                resource_version=revoked.version,
                authorization=authorization,
            )
        return _to_response(revoked)

    @router.post("/records/{record_id}/delete", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_record(
        request_scope: Request,
        record_id: UUID,
        request: DeleteRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> None:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_DELETE,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.REVOKE,
            resource_id=record_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            ledger = MemoryLifecycleLedger(session, context)
            repo = PgMemoryRepository(session, context, ledger=ledger)
            record = await repo.get_by_id(record_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="memory record not found",
                )
            if record.terminal_status():
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_DELETE,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=record_id,
                    error=MemoryTransitionRejected(record.status),
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record is already in terminal status: {record.status.value}",
                )
            reason = "user delete"
            actor_ref = f"user:{actor.principal_id}"
            cascades = build_cascade_events(record, reason)
            deleted = await repo.revoke_record(
                _dedup_key(record), reason, actor_ref=actor_ref
            )
            if deleted is None:  # pragma: no cover - 终态已在上面的前置校验拦截
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"record is already in terminal status: {record.status.value}",
                )
            for event in cascades:
                action = _CASCADE_LEDGER_ACTION.get(event.effect)
                if action is None:
                    continue
                await ledger.record_transition(
                    deleted,
                    action=action,
                    from_status=deleted.status.value,
                    to_status=deleted.status.value,
                    actor_ref=actor_ref,
                    reason=reason,
                )
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_DELETE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=record_id,
                resource_version=deleted.version,
                authorization=authorization,
            )

    @router.post("/conflicts/resolve", response_model=ConflictResponse)
    async def resolve_conflict(
        request_scope: Request,
        request: ResolveRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ConflictResponse:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_RESOLVE,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TEAM_MEMORY,
            policy_action=Action.CONFLICT,
            resource_id=request.conflict_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(session, context)
            conflicts = _derive_conflicts(await repo.list_for_tenant())
        conflict = next(
            (c for c in conflicts if c.conflict_id == request.conflict_id), None
        )
        if conflict is None:
            # 与不存在同形 404（派生冲突集合随记录状态变化，不存在恒等）
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conflict not found or already resolved",
            )
        # fail closed：无持久化冲突实体（DATA_MODEL 无 conflict 表），进程内
        # 标记 resolved 无法跨请求存续——stub 期的 200-resolved 不可复现。
        # 解析 = 对败方记录 correct（supersede），见 S7 §4。
        await append_failed_mutation_audit(
            sessions,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            action=_AUDIT_ACTION_RESOLVE,
            resource_type=_AUDIT_RESOURCE_TYPE,
            resource_id=request.conflict_id,
            error=MemoryTransitionRejected(None),
            request_id=authorization.request_id,
            trace_id=authorization.trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conflict state is not persisted; resolve via record correction",
        )

    @router.get("/conflicts", response_model=list[ConflictResponse])
    async def list_conflicts(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ConflictResponse]:
        context = _tenant(actor)
        await _authorize_read(request_scope, actor, context, context.organization_id)
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(session, context)
            conflicts = _derive_conflicts(await repo.list_for_tenant())
        return [_conflict_response(c) for c in conflicts]

    @router.post("/export", response_model=ExportResponse)
    async def export_records(
        request_scope: Request,
        request: ExportRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ExportResponse:
        context = _tenant(actor)
        await _authorize_read(request_scope, actor, context, context.organization_id)
        scope_enum = MemoryScope(request.scope) if request.scope else None
        type_enum = MemoryType(request.record_type) if request.record_type else None
        status_enum = MemoryStatus(request.status) if request.status else None
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(session, context)
            records = await repo.list_for_user(
                actor.principal_id,
                scope=scope_enum,
                mem_type=type_enum,
                mem_status=status_enum,
            )
        return ExportResponse(
            records=[_to_response(r) for r in records],
            count=len(records),
        )

    @router.get("/stats", response_model=MemoryStatsResponse)
    async def get_stats(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> MemoryStatsResponse:
        context = _tenant(actor)
        await _authorize_read(request_scope, actor, context, context.organization_id)
        async with tenant_session(sessions, context) as session:
            repo = PgMemoryRepository(session, context)
            records = await repo.list_for_user(actor.principal_id)
            conflicts = _derive_conflicts(await repo.list_for_tenant())

        by_status: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for r in records:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            by_scope[r.scope.value] = by_scope.get(r.scope.value, 0) + 1
            by_type[r.type.value] = by_type.get(r.type.value, 0) + 1

        return MemoryStatsResponse(
            total_records=len(records),
            by_status=by_status,
            by_scope=by_scope,
            by_type=by_type,
            unresolved_conflicts=len(conflicts),
        )

    return router


def _dedup_key(record: MemoryRecord) -> DedupKey:
    return DedupKey.from_record(record)


def _conflict_response(conflict: ConflictRecord) -> ConflictResponse:
    return ConflictResponse(
        conflict_id=conflict.conflict_id,
        kind=conflict.kind.value,
        record_a_id=conflict.record_a_id,
        record_b_id=conflict.record_b_id,
        detected_at=conflict.detected_at.isoformat(),
        resolved=conflict.resolved,
        resolved_by=conflict.resolved_by,
        resolved_at=conflict.resolved_at.isoformat() if conflict.resolved_at else None,
    )


def _to_response(record: MemoryRecord) -> MemoryRecordResponse:
    """Convert MemoryRecord to API response model."""
    return MemoryRecordResponse(
        id=record.id,
        version=record.version,
        organization_id=record.organization_id,
        workspace_id=record.workspace_id,
        scope=record.scope.value,
        scope_subject_id=record.scope_subject_id,
        type=record.type.value,
        subject=record.subject,
        key=record.key,
        canonical_value=record.canonical_value,
        source_refs=[
            {
                "source_id": sr.source_id,
                "source_type": sr.source_type,
                "description": sr.description,
            }
            for sr in record.source_refs
        ],
        observed_at=record.observed_at.isoformat(),
        confidence=record.confidence,
        sensitivity=record.sensitivity.value,
        status=record.status.value,
        author_ref=record.author_ref,
        approver_ref=record.approver_ref,
        conflict_refs=[str(c) for c in record.conflict_refs],
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        tombstone=record.tombstone,
    )
