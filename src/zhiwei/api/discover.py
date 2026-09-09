"""S10-T4c：Discover API——S8 Workbench journey 的通用 REST 面（S8 收口补齐，
非 App 专属 solution-pack 面）。

事实源：specs/s8-discover-actions.md §4/§6（Feed/Triage → Case →
Approval/ActionReceipt → HumanResolution；启发式 score 不称 probability；不默认
执行高风险动作）、S8 冻结域（src/zhiwei/discover/*）、handoff
s8-discover-case-action-e2e-exception（解锁条件：真实 API + 前端消费面）。

- GET  /api/v1/discover/feed：RiskHypothesis workbench 投影（status/owner/
  severity/supporting/contradicting/missing/freshness/dedupe；score 以域名词
  逐字呈现，投影层无 probability 语义字段）；
- POST /api/v1/discover/hypotheses/{id}/triage：人工 triage 状态机迁移
  （owner/status；fail closed——非法迁移 409，轨迹落 0018 台账）；
- POST /api/v1/discover/hypotheses/{id}/cases：创建 S8 DiscoverCase（同
  hypothesis 唯一——刷新/重试不复制；S8 Case 聚合与 S6 run-case 面分离）；
- POST /api/v1/discover/cases/{id}/actions：tool action 提交。提交即经
  ActionManager.submit_for_approval 建立 S2 ApprovalRequest 绑定（输入内容
  寻址 digest），action 落 pending_approval 并以 409 逐字拒绝执行——
  server-driven 门禁：高风险动作不默认执行，discover 无自动执行路径；
- POST /api/v1/discover/actions/{id}/approve：审批消费 S2 决定（SoD：
  requester 本人批准由 S2 ApprovalError 拒绝 → 409；进程内决策态缺失同样
  fail closed，不伪造审批）；
- POST /api/v1/discover/cases/{id}/resolutions：HumanResolution 记录（case
  终态迁移；重复记录 409）。

授权/审计纪律：mutation 全部经 authorize_mutation（RUN_CASE_ARTIFACT ×
manage_visible_cases，policy 先于业务事务，allowed 审计与业务同事务、
decision metadata 取真实 OPA 决策）+ 读路径 authorize_read（RUN_CASE_ARTIFACT
× read）。ActionManager 实例随 router 存活（进程内 S2 SoD 决策态）；PG 行是
持久投影，进程重启后 pending approval 的批准 fail closed。
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.discover.actions import ActionManager, ActionType
from zhiwei.discover.hypotheses import HypothesisStatus
from zhiwei.discover.pg_repository import (
    PgDiscoverRepository,
    hypothesis_feed_counts,
    hypothesis_freshness_hours,
)
from zhiwei.discover.resolutions import ResolutionKind
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.models import DiscoverCaseRow
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.roles import Action, Purpose, ResourceType
from zhiwei.runtime.approvals import ApprovalError

# server-driven 门禁的逐字拒绝文本（e2e discover-case-action.spec.ts 断言同串）
APPROVAL_REFUSAL = "action requires human approval before execution"

_AUDIT_TRIAGE = "workspace.discover.triage"
_AUDIT_CASE_CREATE = "workspace.discover.case.create"
_AUDIT_ACTION_SUBMIT = "workspace.discover.action.submit"
_AUDIT_ACTION_APPROVE = "workspace.discover.action.approve"
_AUDIT_RESOLUTION_RECORD = "workspace.discover.resolution.record"


class FeedHypothesisView(BaseModel):
    """feed 投影（0018 discover_hypotheses 行 + case 关联）。机器字段逐字。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str
    description: str
    status: str
    owner: str
    kind: str
    severity: str
    # 启发式 score（域模型 Field 描述 "Heuristic score (NOT a probability)"）——
    # 字段名逐字，本投影不携带任何概率语义字段
    score: float | None
    supporting_count: int
    contradicting_count: int
    missing_count: int
    freshness_hours: float
    dedup_key: str
    suggested_validation_actions: list[str]
    case_id: UUID | None
    created_at: str
    updated_at: str


class TriageRequest(BaseModel):
    """triage 迁移请求（fail closed：未知字段/未知 status 拒绝）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: HypothesisStatus
    owner: str = ""


class DiscoverCaseView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    hypothesis_id: UUID
    hypothesis_ids: list[str]
    title: str
    description: str
    status: str
    severity: str
    owner: str
    dedup_key: str
    created_by: UUID
    created_at: str
    updated_at: str


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = None
    description: str | None = None


class ActionView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    case_id: UUID
    hypothesis_id: UUID
    action_type: str
    tool_name: str
    parameters: dict[str, Any]
    rationale: str
    requested_by: UUID
    status: str
    s2_decision_id: UUID | None
    approved_by: UUID | None
    approval_timestamp: str | None
    created_at: str


class SubmitActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: ActionType
    tool_name: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ResolutionView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    case_id: UUID
    hypothesis_id: UUID
    kind: str
    rationale: str
    resolved_by: UUID
    approved_by: UUID
    notes: str
    evidence_refs: list[str]
    approval_timestamp: str
    created_at: str


class RecordResolutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResolutionKind
    rationale: str = Field(min_length=1)
    notes: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CaseDetailView(DiscoverCaseView):
    actions: list[ActionView]
    resolutions: list[ResolutionView]


def _tenant(actor: ActorContext) -> TenantContext:
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def _feed_view(row: Any, case_id: UUID | None) -> FeedHypothesisView:
    supporting, contradicting, missing = hypothesis_feed_counts(row)
    return FeedHypothesisView(
        id=row.id,
        title=row.title,
        description=row.description,
        status=row.status,
        owner=row.owner,
        kind=row.kind,
        severity=row.severity,
        score=row.score,
        supporting_count=supporting,
        contradicting_count=contradicting,
        missing_count=missing,
        freshness_hours=round(hypothesis_freshness_hours(row), 2),
        dedup_key=row.dedup_key,
        suggested_validation_actions=list(row.suggested_validation_actions or []),
        case_id=case_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _action_view(row: Any) -> ActionView:
    return ActionView(
        id=row.id,
        case_id=row.case_id,
        hypothesis_id=row.hypothesis_id,
        action_type=row.action_type,
        tool_name=row.tool_name,
        parameters=dict(row.parameters or {}),
        rationale=row.rationale,
        requested_by=row.requested_by,
        status=row.status,
        s2_decision_id=row.s2_decision_id,
        approved_by=row.approved_by,
        approval_timestamp=(
            row.approval_timestamp.isoformat() if row.approval_timestamp else None
        ),
        created_at=row.created_at.isoformat(),
    )


def _resolution_view(row: Any) -> ResolutionView:
    return ResolutionView(
        id=row.id,
        case_id=row.case_id,
        hypothesis_id=row.hypothesis_id,
        kind=row.kind,
        rationale=row.rationale,
        resolved_by=row.resolved_by,
        approved_by=row.approved_by,
        notes=row.notes,
        evidence_refs=list(row.evidence_refs or []),
        approval_timestamp=row.approval_timestamp.isoformat(),
        created_at=row.created_at.isoformat(),
    )


def _case_view(row: Any) -> DiscoverCaseView:
    return DiscoverCaseView(
        id=row.id,
        hypothesis_id=row.hypothesis_id,
        hypothesis_ids=list(row.hypothesis_ids or []),
        title=row.title,
        description=row.description,
        status=row.status,
        severity=row.severity,
        owner=row.owner,
        dedup_key=row.dedup_key,
        created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


async def _case_detail(
    repository: PgDiscoverRepository, case_id: UUID
) -> CaseDetailView | None:
    case_row = await repository.get_case_row(case_id)
    if case_row is None:
        return None
    action_rows = await repository.list_action_rows_for_case(case_id)
    resolution_rows = await repository.list_resolution_rows_for_case(case_id)
    return CaseDetailView(
        **_case_view(case_row).model_dump(),
        actions=[_action_view(action) for action in action_rows],
        resolutions=[_resolution_view(resolution) for resolution in resolution_rows],
    )


def create_discover_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: Any,
) -> APIRouter:
    """policy_enforcer 是生产纵切的必需注入（缺失在构造期拒绝，fail closed）。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")

    # S2 SoD 决策态随 router 存活（进程内）；PG 行是持久投影，进程重启后的
    # pending 批准 fail closed（S2 决定不可重建——绝不伪造审批）。
    action_manager = ActionManager()

    router = APIRouter(prefix="/api/v1/discover", tags=["discover"])

    @router.get("/feed", response_model=list[FeedHypothesisView])
    async def get_feed(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[FeedHypothesisView]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        # 读路径 PEP（ADR-012 决策 4）：读 cell 取冻结矩阵 run_case_artifact.read
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.READ,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            rows = await repository.list_hypothesis_rows()
            # case 关联按 hypothesis 反查（一 hypothesis 至多一条 case，0018 唯一索引）
            case_rows = (
                await session.scalars(
                    select(DiscoverCaseRow).where(
                        DiscoverCaseRow.organization_id == context.organization_id,
                        DiscoverCaseRow.workspace_id == context.workspace_id,
                    )
                )
            ).all()
            case_by_hypothesis = {row.hypothesis_id: row.id for row in case_rows}
        return [_feed_view(row, case_by_hypothesis.get(row.id)) for row in rows]

    @router.post("/hypotheses/{hypothesis_id}/triage", response_model=FeedHypothesisView)
    async def triage_hypothesis(
        hypothesis_id: UUID,
        request: TriageRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> FeedHypothesisView:
        del idempotency_key  # PEP 契约要求 mutation 携带（重放由状态机 409 拒绝）
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_TRIAGE,
            resource_type="discover_hypothesis",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=hypothesis_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            row = await repository.get_hypothesis_row(hypothesis_id)
            if row is None:
                # 未知与跨租户 hypothesis 同语义 404（防枚举）
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="hypothesis not found"
                )
            try:
                row = await repository.apply_triage(
                    row,
                    to_status=request.status,
                    owner=request.owner,
                    actor_ref=f"user:{actor.principal_id}",
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_TRIAGE,
                resource_type="discover_hypothesis",
                resource_id=hypothesis_id,
                resource_version=1,
                authorization=authorization,
            )
        return _feed_view(row, None)

    @router.post(
        "/hypotheses/{hypothesis_id}/cases",
        status_code=status.HTTP_201_CREATED,
        response_model=DiscoverCaseView,
    )
    async def create_case(
        hypothesis_id: UUID,
        request: CreateCaseRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> DiscoverCaseView:
        del idempotency_key  # 重放由同 hypothesis 唯一索引 409 兜底
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        case_id = uuid4()
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_CASE_CREATE,
            resource_type="discover_case",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=case_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            hypothesis_row = await repository.get_hypothesis_row(hypothesis_id)
            if hypothesis_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="hypothesis not found"
                )
            try:
                case_row = await repository.create_case_for_hypothesis(
                    hypothesis_row,
                    created_by=actor.principal_id,
                    title=request.title,
                    description=request.description,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_CASE_CREATE,
                resource_type="discover_case",
                resource_id=case_row.id,
                resource_version=1,
                authorization=authorization,
            )
        return _case_view(case_row)

    @router.get("/cases/{case_id}", response_model=CaseDetailView)
    async def get_case(
        case_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> CaseDetailView:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.READ,
            resource_id=case_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            detail = await _case_detail(PgDiscoverRepository(session, context), case_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="case not found"
            )
        return detail

    @router.post("/cases/{case_id}/actions")
    async def submit_action(
        case_id: UUID,
        request: SubmitActionRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> None:
        """tool action 提交：request 落账（pending_approval + S2 决定绑定），
        执行被 server-driven 门禁逐字拒绝（discover 无自动执行路径——S8 §9
        不默认执行高风险动作）。409 是「已提交未执行」的机器可读语义。"""
        del idempotency_key  # 重放由 (case, input_digest) 唯一索引 409 兜底
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        action_id = uuid4()
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_SUBMIT,
            resource_type="discover_action",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=action_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            case_row = await repository.get_case_row(case_id)
            if case_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="case not found"
                )
            # ActionManager 复用 S2 审批机制：requester/modifier = 提交人、
            # effective agent identity = discover；S2 决定绑定由 manager 内部建立
            # （SoD 与 digest 契约的唯一事实实现），PG 行落 decision id 投影。
            action_request = action_manager.create_request(
                hypothesis_id=case_row.hypothesis_id,
                action_type=request.action_type,
                tool_name=request.tool_name,
                rationale=request.rationale,
                requested_by=str(actor.principal_id),
                case_id=case_id,
                parameters=request.parameters,
            )
            action_request = action_manager.submit_for_approval(action_request.id)
            # S2 决定查询走 manager 公共访问器（decision id / input digest 的
            # 单一事实源），PG 行落投影；行 id = 域 request id（同标识）
            decision = action_manager.s2_decision(action_request.id)
            try:
                row = await repository.create_action(
                    action_id=action_request.id,
                    hypothesis_id=case_row.hypothesis_id,
                    case_id=case_id,
                    action_type=request.action_type.value,
                    tool_name=request.tool_name,
                    parameters=request.parameters,
                    rationale=request.rationale,
                    requested_by=actor.principal_id,
                    s2_decision_id=decision.id,
                    input_digest=decision.input_digest,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            except IntegrityError as exc:
                # 并发重放的唯一索引兜底（应用层预检先行）
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="duplicate action submission",
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_SUBMIT,
                resource_type="discover_action",
                resource_id=row.id,
                resource_version=1,
                authorization=authorization,
            )
        # request 已落账；执行被门禁拒绝——409 携带逐字拒绝文本（e2e 断言同串）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=APPROVAL_REFUSAL
        )

    @router.post("/actions/{action_id}/approve", response_model=ActionView)
    async def approve_action(
        action_id: UUID,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ActionView:
        del idempotency_key  # 重放由状态机 409 拒绝（approved 不可再批准）
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_APPROVE,
            resource_type="discover_action",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=action_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            row = await repository.get_action_row(action_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="action not found"
                )
            # 审批决定经 S2 ApprovalRequestManager.approve 产生——requester 本人
            # 批准由 S2 拒绝（ApprovalError）；本端点只消费已批准的决定。
            try:
                action_manager.approve(action_id, approved_by=str(actor.principal_id))
            except (ApprovalError, ValueError) as exc:
                # ValueError：进程内决策态缺失（router 重建/重启）——同样 fail closed
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            try:
                row = await repository.approve_action(
                    row, approved_by=actor.principal_id
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_APPROVE,
                resource_type="discover_action",
                resource_id=row.id,
                resource_version=1,
                authorization=authorization,
            )
        return _action_view(row)

    @router.post(
        "/cases/{case_id}/resolutions",
        status_code=status.HTTP_201_CREATED,
        response_model=ResolutionView,
    )
    async def record_resolution(
        case_id: UUID,
        request: RecordResolutionRequest,
        request_scope: Request,
        idempotency_key: Annotated[
            str, Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key")
        ],
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ResolutionView:
        del idempotency_key  # 重放由 case 终态状态机 409 拒绝
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        resolution_id = uuid4()
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_RESOLUTION_RECORD,
            resource_type="discover_resolution",
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.MANAGE_VISIBLE_CASES,
            resource_id=resolution_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            repository = PgDiscoverRepository(session, context)
            case_row = await repository.get_case_row(case_id)
            if case_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="case not found"
                )
            try:
                row = await repository.record_resolution(
                    case_row,
                    hypothesis_id=case_row.hypothesis_id,
                    kind=request.kind.value,
                    rationale=request.rationale,
                    resolved_by=actor.principal_id,
                    approved_by=actor.principal_id,
                    notes=request.notes,
                    evidence_refs=request.evidence_refs,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_RESOLUTION_RECORD,
                resource_type="discover_resolution",
                resource_id=row.id,
                resource_version=1,
                authorization=authorization,
            )
        return _resolution_view(row)

    return router
