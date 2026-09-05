"""S9-T7 补口：EvalRun API——租户内列表/详情、resume/seal 治理动作与密封报告。

工厂模式与 api/releases.py / api/claims.py 同型：组合期注入 actor 依赖 /
session factory / policy enforcer / object store，端点内不做授权语义决策
（PEP 判定统一走 policy_gate）。

路径取 `/api/v1/evals`（不带 `/runs` 段）：前端 apps/web/src/features/evals 的
fetch 路径与本 router 一一对应（GET /api/v1/evals、GET/POST /api/v1/evals/{id}
及其 /resume /seal /report 子路径），e2e mock（eval-release-observability.spec.ts）
按同一资源名推导——API 与前端契约 1:1，不另造第二套资源名。

策略 cell（冻结矩阵 docs/PERMISSIONS.md §3.1，不新增 cell）：eval run 是发布侧
证据链的一环，读 cell 复用 agent_publish.read_manifest；resume/seal 与 claim
register/upgrade 同性质（提交/补交密封证据），复用 agent_publish.request。

响应纪律：
- 样本 outcome 默认 metadata-only：只回 status + result_digest，result 正文
  （prompt/completion）永不进响应——与前端「正文永不进 DOM」纪律同源。
- 详情里 report 恒为 null：报告的 scope 标签（model/version/date/corpus/
  environment）只能由调用方显式声明（evals/reports.py 禁止猜测），详情无法
  诚实填充；报告经 GET .../report 按显式 scope 现取（同 eval report CLI）。
- 业务拒绝（sealed 无出边、未完备不可 seal、未 sealed 不可出报告）→ 409
  {"reason", "message"} 机器可读拒绝面，前端按 reason 分支。
"""

# 注意：本模块【不用】from __future__ import annotations——endpoint 签名里的
# `Annotated[ActorContext, Depends(actor_dependency)]` 引用工厂闭包变量，必须
# 在 def 期立即求值才能被 FastAPI 解析（见 api/observability.py 同款说明）。

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.evals.domain import (
    TERMINAL_STATUSES,
    EvalMode,
    RegisteredUnit,
    SampleOutcome,
    SampleStatus,
)
from zhiwei.evals.reports import EvalReportRefused, EvalReportScopeInput, build_eval_report
from zhiwei.evals.runs import (
    EvalFoundationService,
    EvalRunNotFound,
    EvalStateError,
    RunPhase,
)
from zhiwei.evals.sealing import EvalSealRefused
from zhiwei.identity.domain import ActorContext
from zhiwei.object_store.manifests import ArtifactVerificationError
from zhiwei.object_store.ports import ObjectStore
from zhiwei.persistence.models import ArtifactManifest, EvalRun, EvalSample
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType

# 详情 status_breakdown 的固定键序：terminal 四态在前（完整分母口径），
# registered/running 在后；与 evals/domain.py 的封闭状态集一一对应。
_BREAKDOWN_STATUSES = (
    SampleStatus.COMPLETED,
    SampleStatus.FAILED,
    SampleStatus.REFUSED,
    SampleStatus.ERROR,
    SampleStatus.REGISTERED,
    SampleStatus.RUNNING,
)

_TERMINAL_VALUES = tuple(item.value for item in TERMINAL_STATUSES)


class SealEvalRunRequest(BaseModel):
    """seal 请求体与 EvalFoundationService.seal 签名逐参数对齐，不弱化：
    migration_revision 与 test_report 都是必需输入（密封件引用迁移基线与
    S0 eval 契约测试证据，缺任一都无法构成可复核的密封）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    migration_revision: str
    test_report: dict[str, Any]


class EvalRunListItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: str
    run_id: str | None
    mode: str
    status: str
    sealed_at: str | None
    registered_units: int
    terminal_units: int
    campaign_id: str | None
    prereg_manifest_id: str | None
    model_manifest_id: str | None
    source_manifest_id: str | None
    attempt_manifest_id: str | None
    created_at: str


class EvalOutcomeView(BaseModel):
    """样本 outcome 的 metadata-only 投影；result 正文刻意不在模型里。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: RegisteredUnit
    status: str
    result_digest: str | None


class EvalRunStatusBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    completed: int
    failed: int
    refused: int
    error: int
    registered: int
    running: int


class EvalRunDetailView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: str
    run_id: str | None
    mode: str
    status: str
    sealed_at: str | None
    registered_units: list[RegisteredUnit]
    outcomes: list[EvalOutcomeView]
    status_breakdown: EvalRunStatusBreakdown
    # 恒 None：报告需要调用方显式 scope 声明，详情不猜测（取报告走 /report）。
    report: dict[str, Any] | None = None


def _tenant(actor: ActorContext) -> TenantContext:
    if actor.organization_id is None or actor.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization and workspace context required",
        )
    return TenantContext(
        organization_id=actor.organization_id, workspace_id=actor.workspace_id
    )


def _refusal(reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"reason": reason, "message": message}
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="eval run not found"
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _load_row(
    session: AsyncSession, context: TenantContext, eval_run_id: UUID
) -> EvalRun:
    row = await session.scalar(
        select(EvalRun).where(
            EvalRun.id == eval_run_id,
            EvalRun.organization_id == context.organization_id,
            EvalRun.workspace_id == context.workspace_id,
        )
    )
    if row is None:
        raise EvalRunNotFound("EvalRun is missing from tenant scope")
    return row


async def _detail_view(
    session: AsyncSession, context: TenantContext, eval_run_id: UUID
) -> EvalRunDetailView:
    """租户内读取单条 EvalRun 的详情投影；域外/损坏数据 fail closed。

    与 EvalFoundationService._load_state 同口径：mode/status 不是封闭集内的
    值即拒绝，不悄悄透传未知枚举。
    """
    row = await _load_row(session, context, eval_run_id)
    try:
        mode = EvalMode(row.mode)
        run_status = RunPhase(row.status)
    except ValueError as exc:
        raise EvalStateError(f"eval run row is inconsistent: {exc}") from exc
    sample_rows = (
        await session.scalars(
            select(EvalSample)
            .where(
                EvalSample.organization_id == context.organization_id,
                EvalSample.workspace_id == context.workspace_id,
                EvalSample.eval_run_id == eval_run_id,
            )
            .order_by(EvalSample.sample_id, EvalSample.unit_id)
        )
    ).all()
    counts: dict[SampleStatus, int] = dict.fromkeys(_BREAKDOWN_STATUSES, 0)
    outcomes: list[EvalOutcomeView] = []
    for sample in sample_rows:
        try:
            sample_status = SampleStatus(sample.status)
        except ValueError as exc:
            raise EvalStateError(
                f"eval sample status is unknown: {sample.status!r}"
            ) from exc
        counts[sample_status] += 1
        # outcomes 与域定义同口径（SampleOutcome 即终态）：registered/running
        # 是「尚无结果」，不进 outcomes——前端据此渲染 "no outcome"。
        if sample_status in TERMINAL_STATUSES:
            outcomes.append(
                EvalOutcomeView(
                    unit=RegisteredUnit(sample_id=sample.sample_id, unit_id=sample.unit_id),
                    status=sample_status.value,
                    result_digest=sample.result_digest,
                )
            )
    return EvalRunDetailView(
        eval_run_id=str(row.id),
        run_id=str(row.run_id) if row.run_id is not None else None,
        mode=mode.value,
        status=run_status.value,
        sealed_at=_iso(row.sealed_at),
        registered_units=[
            RegisteredUnit(sample_id=sample.sample_id, unit_id=sample.unit_id)
            for sample in sample_rows
        ],
        outcomes=outcomes,
        status_breakdown=EvalRunStatusBreakdown(
            completed=counts[SampleStatus.COMPLETED],
            failed=counts[SampleStatus.FAILED],
            refused=counts[SampleStatus.REFUSED],
            error=counts[SampleStatus.ERROR],
            registered=counts[SampleStatus.REGISTERED],
            running=counts[SampleStatus.RUNNING],
        ),
    )


async def _list_views(session: AsyncSession, context: TenantContext) -> list[EvalRunListItem]:
    rows = (
        (
            await session.scalars(
                select(EvalRun)
                .where(
                    EvalRun.organization_id == context.organization_id,
                    EvalRun.workspace_id == context.workspace_id,
                )
                .order_by(EvalRun.created_at, EvalRun.id)
            )
        )
        .all()
    )
    # 单条分组聚合补每 run 的 registered/terminal 计数；terminal 集合取
    # evals/domain.py 的封闭定义，不在 API 层另立状态清单。
    sample_counts = {
        eval_run_id: (int(total), int(terminal))
        for eval_run_id, total, terminal in (
            await session.execute(
                select(
                    EvalSample.eval_run_id,
                    func.count(),
                    func.sum(
                        case(
                            (EvalSample.status.in_(_TERMINAL_VALUES), 1),
                            else_=0,
                        )
                    ),
                )
                .where(
                    EvalSample.organization_id == context.organization_id,
                    EvalSample.workspace_id == context.workspace_id,
                )
                .group_by(EvalSample.eval_run_id)
            )
        ).all()
    }
    views: list[EvalRunListItem] = []
    for row in rows:
        try:
            mode = EvalMode(row.mode)
            run_status = RunPhase(row.status)
        except ValueError as exc:
            raise EvalStateError(f"eval run row is inconsistent: {exc}") from exc
        registered, terminal = sample_counts.get(row.id, (0, 0))
        views.append(
            EvalRunListItem(
                eval_run_id=str(row.id),
                run_id=str(row.run_id) if row.run_id is not None else None,
                mode=mode.value,
                status=run_status.value,
                sealed_at=_iso(row.sealed_at),
                registered_units=registered,
                terminal_units=terminal,
                campaign_id=str(row.campaign_id) if row.campaign_id is not None else None,
                prereg_manifest_id=(
                    str(row.prereg_manifest_id) if row.prereg_manifest_id is not None else None
                ),
                model_manifest_id=(
                    str(row.model_manifest_id) if row.model_manifest_id is not None else None
                ),
                source_manifest_id=(
                    str(row.source_manifest_id) if row.source_manifest_id is not None else None
                ),
                attempt_manifest_id=(
                    str(row.attempt_manifest_id)
                    if row.attempt_manifest_id is not None
                    else None
                ),
                created_at=_iso(row.created_at) or "",
            )
        )
    return views


async def _report_payload(
    session: AsyncSession,
    context: TenantContext,
    store: ObjectStore,
    eval_run_id: UUID,
    scope: EvalReportScopeInput,
) -> dict[str, Any]:
    """密封报告构建：与 eval report CLI（cli/evals.py _report_flow）同一组合——
    verify_sealed 独立复核密封件 + 原始 outcome 逐项比对 digest，不信任调用方
    或缓存结论；scope 标签全显式传入，不从环境/时间猜测。not_sealed 拒绝面由
    端点先行判定；verify_sealed 的域层校验是防篡改纵深，不是该 reason 的来源。
    """
    service = EvalFoundationService(session, context, store)
    artifact = await service.verify_sealed(eval_run_id)
    seal_manifest = await session.scalar(
        select(ArtifactManifest).where(
            ArtifactManifest.organization_id == context.organization_id,
            ArtifactManifest.workspace_id == context.workspace_id,
            ArtifactManifest.owner_resource_type == "eval_run",
            ArtifactManifest.owner_resource_id == eval_run_id,
        )
    )
    if seal_manifest is None:
        raise ArtifactVerificationError("seal manifest is missing")
    sample_rows = (
        await session.scalars(
            select(EvalSample)
            .where(
                EvalSample.organization_id == context.organization_id,
                EvalSample.workspace_id == context.workspace_id,
                EvalSample.eval_run_id == eval_run_id,
            )
            .order_by(EvalSample.sample_id, EvalSample.unit_id)
        )
    ).all()
    outcomes = [
        SampleOutcome(
            unit=RegisteredUnit(sample_id=sample.sample_id, unit_id=sample.unit_id),
            status=SampleStatus(sample.status),
            result=dict(sample.result or {}),
        )
        for sample in sample_rows
    ]
    report, _digest = build_eval_report(
        artifact,
        outcomes,
        seal_digest=seal_manifest.content_digest,
        scope=scope,
    )
    return report.canonical_mapping()


def create_evals_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
    object_store: ObjectStore,
) -> APIRouter:
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/evals", tags=["evals"])

    @router.get("", response_model=list[EvalRunListItem])
    async def list_eval_runs(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[EvalRunListItem]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        # 读路径 PEP（ADR-012 决策 4）：eval 证据读复用 agent_publish.read_manifest
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            return await _list_views(session, context)

    @router.get("/{eval_run_id}", response_model=EvalRunDetailView)
    async def get_eval_run(
        eval_run_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> EvalRunDetailView:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=eval_run_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            try:
                return await _detail_view(session, context, eval_run_id)
            except EvalRunNotFound:
                raise _not_found() from None
            except EvalStateError as error:
                raise _refusal("eval_state_error", str(error)) from error

    @router.post("/{eval_run_id}/resume", response_model=EvalRunDetailView)
    async def resume_eval_run(
        eval_run_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> EvalRunDetailView:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        # 矩阵：agent_publish.request（builder/workspace_admin 提交侧动作）
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="eval.run.resume",
            resource_type="eval_run",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=eval_run_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            service = EvalFoundationService(session, context, object_store)
            try:
                await service.resume(eval_run_id)
            except EvalRunNotFound:
                raise _not_found() from None
            except EvalStateError as error:
                # 业务拒绝：独立事务 failed 审计 + 机器可读 409（同 releases 形）
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="eval.run.resume",
                    resource_type="eval_run",
                    resource_id=eval_run_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal("eval_state_error", str(error)) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="eval.run.resume",
                resource_type="eval_run",
                resource_id=eval_run_id,
                resource_version=1,
                authorization=authorization,
            )
            return await _detail_view(session, context, eval_run_id)

    @router.post("/{eval_run_id}/seal", response_model=EvalRunDetailView)
    async def seal_eval_run(
        eval_run_id: UUID,
        request: SealEvalRunRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> EvalRunDetailView:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="eval.run.seal",
            resource_type="eval_run",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=eval_run_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            service = EvalFoundationService(session, context, object_store)
            try:
                await service.seal(
                    eval_run_id,
                    migration_revision=request.migration_revision,
                    test_report=request.test_report,
                )
            except EvalRunNotFound:
                raise _not_found() from None
            except EvalSealRefused as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="eval.run.seal",
                    resource_type="eval_run",
                    resource_id=eval_run_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal("eval_seal_refused", str(error)) from error
            except EvalStateError as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="eval.run.seal",
                    resource_type="eval_run",
                    resource_id=eval_run_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise _refusal("eval_state_error", str(error)) from error
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="eval.run.seal",
                resource_type="eval_run",
                resource_id=eval_run_id,
                resource_version=1,
                authorization=authorization,
            )
            return await _detail_view(session, context, eval_run_id)

    @router.get("/{eval_run_id}/report")
    async def get_eval_run_report(
        eval_run_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        model_label: Annotated[str, Query(alias="model")],
        version: Annotated[str, Query()],
        date_label: Annotated[str, Query(alias="date")],
        corpus: Annotated[str, Query()],
        environment: Annotated[str, Query()],
    ) -> dict[str, Any]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.READ_MANIFEST,
            resource_id=eval_run_id,
            trace_id=trace_id,
        )
        # scope 标签是报告的声明口径（同 eval report CLI 的必填 flags）：缺失或
        # 非法在请求校验层拒绝，绝不代填默认值
        try:
            scope = EvalReportScopeInput(
                model=model_label,
                version=version,
                date=date_label,
                corpus=corpus,
                environment=environment,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        async with tenant_session(sessions, context) as session:
            try:
                row = await _load_row(session, context, eval_run_id)
            except EvalRunNotFound:
                raise _not_found() from None
            # not_sealed 是本端点的机器可读拒绝面（deliverable 冻结 reason 码）；
            # 后续 verify_sealed 的域层校验是防篡改纵深，不是该 reason 的来源。
            if row.status != RunPhase.SEALED.value:
                raise _refusal("not_sealed", "eval run report requires a sealed run")
            try:
                return await _report_payload(session, context, object_store, eval_run_id, scope)
            except EvalRunNotFound:
                raise _not_found() from None
            except EvalStateError as error:
                raise _refusal("eval_state_error", str(error)) from error
            except EvalReportRefused as error:
                raise _refusal("eval_report_refused", str(error)) from error
            except ArtifactVerificationError as error:
                raise _refusal("seal_verification_failed", str(error)) from error

    return router
