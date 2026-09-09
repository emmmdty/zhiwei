"""S2-T7：Run API——REST 投影绑定 PG 真相 + Planner port + 审批决策端点。

事实源：specs/s2-agent-runtime.md §3/§5（REST projection 可恢复；sandbox run）+
2026-09-03 增补（ADR-012）：POST /runs 的 body workspace_id 必须经成员校验
（客户端声明只是请求，不是授权事实）；审批 requester 穿透为创建者 principal。

- GET /runs / GET /runs/{id} / GET /runs/{id}/events：从 PG canonical events
  reduce（刷新/断网恢复的权威来源），无进程内缓存；读路径经 PEP
  run_case_artifact.read cell（ADR-012 决策 4：runs 读走 PEP，RLS+membership
  只是纵深防御）；
- POST /runs：workspace 归属（404 防枚举）→ 成员校验（403）→ Planner port 产出图
  → RunCommandService（Run 行 + outbox 命令同事务，requested_by=创建者 principal）
  → 请求内联 dispatch（S2 单进程形态；多租户后台 dispatcher 属 S11）；
- POST /runs/{id}/approvals/{request_id}/decision：ApprovalRequestStore 的 CAS +
  SoD 守护，决策经命令路径投递给 workflow（approval_decided 信号）。PEP 接
  tool_approval.approve/reject cell，当事人证据（requester/modifier）从 approval
  权威记录解析后经 resource_context 通道传入（F-R3-06）；非 USER principal 显式
  拒绝（F-R2-13）；决策写 allowed（同事务）/ denied・failed（独立事务）审计。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    denied_audit_record,
    request_trace,
)
from zhiwei.identity.audit import append_fail_closed_audit
from zhiwei.identity.commands import canonical_request_digest
from zhiwei.identity.domain import ActorContext, PrincipalKind
from zhiwei.identity.sessions import MembershipScopeError
from zhiwei.persistence.approvals import ApprovalRequestStore
from zhiwei.persistence.outbox import OutboxSink
from zhiwei.persistence.repositories import IdempotencyConflict, TenantRepository
from zhiwei.persistence.run_commands import RunCommandError, RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.input import ResourceContext
from zhiwei.policy.roles import Action, Purpose, ResourceType
from zhiwei.runtime.approvals import ApprovalError
from zhiwei.runtime.planner import FixturePlanner, PlanIntent, Planner, PlannerError
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE
from zhiwei.workers.outbox_dispatcher import OutboxDispatcher
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

# F-R5-05（T-P4.4）：POST /runs 的幂等 scope（idempotency_records 键空间 =
# (org, ws, scope, key)，workspace 可空）
IDEMPOTENCY_SCOPE_RUN_START = "run.start"
logger = logging.getLogger(__name__)

_AUDIT_ACTION_DECIDE = "run.approval.decide"
_AUDIT_RESOURCE_TYPE = "approval_request"

WorkspaceAuthorizer = Callable[[ActorContext, UUID], Awaitable[None]]
"""成员校验端口：确认 actor 持有目标 workspace 的 membership，否则抛
MembershipScopeError。生产组装绑定 SessionService.resolve_context（S1 权威
membership 解析）；缺失在构造期拒绝（fail closed）。"""


class RunRecord(BaseModel):
    """一个 run 的列表项投影（来自 PG reduce）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    organization_id: UUID
    # S10 fix-A（D2）：caller-declared planner 意图（创建期持久化，0019）。
    # 无意图（eval 直连/存量行）→ None——web 绑定解析如实渲染 "No app binding"。
    template: str | None = None


class RunDetail(BaseModel):
    """run 详情（含任务投影）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    organization_id: UUID
    tasks: dict[str, dict[str, Any]] = {}
    template: str | None = None
    # 执行模式标注（fixture 资格诚实性，R1 D4）：template 非空 → 该 run 的
    # 图由 fixture planner / pack 计划源产出——今天的 POST /runs 一切
    # origination 都是 fixture 绑定执行（无 live source），template 列即这一
    # 事实的持久化标记；template 缺失 → None（不猜）。S9 模型驱动 planner
    # 落地时必须改为 planner 声明的执行模式，不得沿用本推导。
    mode: str | None = None


class CreateRunRequest(BaseModel):
    """POST /runs 的请求体（planner 意图）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # max_length 与 0019 的 runs.template 列对齐：越界在边界拒绝，不是 DB 报错
    template: Annotated[str, Field(max_length=64)] = "single-fixture"
    workspace_id: UUID


class ApprovalDecisionRequest(BaseModel):
    """审批决策请求（fail-closed：decision 枚举校验）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: str
    reason: str = ""


class ApprovalRequestView(BaseModel):
    """审批请求投影。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    run_id: UUID
    task_id: str
    status: str
    requester: str


class DecisionResult(BaseModel):
    """审批决策结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    decision: str
    accepted: bool


def _tenant(actor: ActorContext, workspace_id: UUID | None = None) -> TenantContext:
    if actor.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization context required",
        )
    ws = workspace_id or actor.workspace_id
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="workspace context required",
        )
    return TenantContext(organization_id=actor.organization_id, workspace_id=ws)


def _fixture_mode(template: str | None) -> str | None:
    """执行模式标注（fixture 资格诚实性，R1 D4）。

    今天的 POST /runs 一切 origination——fixture 模板与 pack 模板计划源——产出的
    都是 fixture 绑定执行（零 live source）；template 列是「经此两条 origination
    创建」的持久化标记，据此派生 mode=fixture，不发明事件之外的事实。template
    缺失（eval 直连/存量行）→ None。S9 模型驱动 planner 落地时本推导必须换为
    planner 声明的执行模式（登记的跟踪项，不得沿用）。
    """
    return "fixture" if template is not None else None


def _parse_principal(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _approval_evidence(record: Any) -> ResourceContext:
    """从 approval 权威记录解析 SoD 当事人证据（PEP 职责：不信任 caller 自述）。

    requester/last_input_modifier/agent_identity 在 store 层是字符串化 principal；
    eval 直连路径可能为 "system" 等非 UUID 常量——可解析才进入证据。approval
    动作被 PolicyInput 边界强制 requester 证据，缺席即 fail closed（403 policy
    denied）；Rego approval_by_party 的当事人集合同步收窄，两层防线一致。
    """
    requester = _parse_principal(record.requester)
    modifier = _parse_principal(record.last_input_modifier)
    return ResourceContext(
        requester_principal_id=requester,
        modifier_principal_ids=(modifier,) if modifier is not None else (),
        agent_identity_principal_id=_parse_principal(record.agent_identity),
    )


def create_runs_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions_factory: Callable[[ActorContext, UUID | None], async_sessionmaker[AsyncSession]],
    temporal_target: str,
    planner: Planner | None = None,
    dispatch_inline: bool = True,
    task_queue: str = DEFAULT_TASK_QUEUE,
    workspace_authorizer: WorkspaceAuthorizer,
    event_sink: OutboxSink | None = None,
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """Run API router。

    sessions_factory：actor+workspace → PG session factory（app 组装期绑定）；
    temporal_target：Temporal 前端地址（local-product 默认 dev server）；
    dispatch_inline：命令提交后在同一请求内跑一轮 dispatcher poll（S2 单进程
    形态，见 docstring）；
    workspace_authorizer：POST /runs 的 body workspace 成员校验（fail closed，
    缺失在构造期拒绝——客户端声明不是授权事实，ADR-012）；
    event_sink：canonical 事件的增量通道（Redis；生产组装必须接线，否则
    「加速通道」为死代码、SSE 退化为纯轮询——spec §4 增补，ADR-012 反例）；
    policy_enforcer：读路径 run_case_artifact.read + 决策端点 tool_approval
    cells 的 PEP（fail closed，缺失在构造期拒绝——F-R3-02/F-R2-13）。
    """
    if workspace_authorizer is None:
        raise TypeError("workspace_authorizer must be provided (fail closed)")
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    planner = planner or FixturePlanner()
    router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

    def _dispatcher(sessions, context: TenantContext, client) -> OutboxDispatcher:
        from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
        from zhiwei.workers.outbox_dispatcher import (
            OutboxDispatcherConfig,
            SessionOutboxRepository,
        )

        return OutboxDispatcher(
            SessionOutboxRepository(sessions, context),
            OutboxSignalHandler(TemporalWorkflowSender(client)),
            OutboxDispatcherConfig(
                worker_id=f"api-{context.organization_id}",
                poll_interval=timedelta_safe(),
                batch_limit=20,
                max_attempts=5,
                base_delay=timedelta_safe(),
            ),
            event_sink=event_sink,
        )

    async def _dispatch(session_factory, context: TenantContext) -> None:
        if not dispatch_inline:
            return
        from temporalio.client import Client

        try:
            client = await Client.connect(temporal_target)
        except Exception:
            # dispatch 失败不回滚命令——outbox 保留 pending，由后台 dispatcher 重试
            logger.warning("temporal unavailable; command stays pending in outbox")
            return
        try:
            dispatcher = _dispatcher(session_factory, context, client)
            for _ in range(10):
                await dispatcher.poll_once()
                await asyncio.sleep(0.02)
        finally:
            # Client 无显式 close 也可被 GC，但显式断开更干净
            pass

    async def _authorize_run_read(
        request_scope: Request,
        actor: ActorContext,
        context: TenantContext,
        resource_id: UUID,
    ) -> None:
        """runs 读路径 PEP：run_case_artifact.read cell（ADR-012 决策 4）。

        读不写审计（与 authorize_read 全仓语义一致）；deny/租户不匹配/OPA 不可达
        → 403，不触碰数据。cell 的矩阵语义（agent_builder workspace 作用域 /
        auditor org 作用域）由 Rego 侧裁决。
        """
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.RUN_CASE_ARTIFACT,
            policy_action=Action.READ,
            resource_id=resource_id,
            trace_id=trace_id,
        )

    @router.get("", response_model=list[RunRecord])
    async def list_runs(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[RunRecord]:
        context = _tenant(actor)
        await _authorize_run_read(request_scope, actor, context, context.organization_id)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            from sqlalchemy import select

            from zhiwei.persistence.models import Run

            rows = (
                await session.scalars(
                    select(Run).where(
                        Run.organization_id == context.organization_id,
                        Run.workspace_id == context.workspace_id,
                    )
                )
            ).all()
            records = []
            for row in rows:
                state = await store.reduce_state(row.id)
                records.append(
                    RunRecord(
                        run_id=row.id,
                        status=state.status,
                        organization_id=context.organization_id,
                        template=row.template,
                    )
                )
        return records

    @router.get("/{run_id}", response_model=RunDetail)
    async def get_run(
        request_scope: Request,
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> RunDetail:
        context = _tenant(actor)
        await _authorize_run_read(request_scope, actor, context, run_id)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
            if state.graph is None and state.status == "created":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                )
            # template 只住在 Run 行（创建期持久化，0019）——canonical events
            # 不携带 planner 意图；跨租户/不存在的 run 统一 404（防枚举）。
            from sqlalchemy import select

            from zhiwei.persistence.models import Run

            run_row = await session.scalar(
                select(Run).where(
                    Run.id == run_id,
                    Run.organization_id == context.organization_id,
                    Run.workspace_id == context.workspace_id,
                )
            )
            if run_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                )
            tasks = {
                tid: {"status": t.status, "error": t.error}
                for tid, t in state.tasks.items()
            }
        return RunDetail(
            run_id=run_id,
            status=state.status,
            organization_id=context.organization_id,
            tasks=tasks,
            template=run_row.template,
            mode=_fixture_mode(run_row.template),
        )

    @router.get("/{run_id}/events")
    async def get_run_events(
        request_scope: Request,
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[dict[str, Any]]:
        context = _tenant(actor)
        await _authorize_run_read(request_scope, actor, context, run_id)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            pairs = await store.load_events_with_sequences(run_id)
            if not pairs:
                from sqlalchemy import select

                from zhiwei.persistence.models import Run

                exists = await session.scalar(
                    select(Run.id).where(
                        Run.id == run_id,
                        Run.organization_id == context.organization_id,
                        Run.workspace_id == context.workspace_id,
                    )
                )
                if exists is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
                    )
        return [
            {
                "sequence_no": seq,
                "event_type": type(event).__name__,
                "event_id": str(event.event_id),
                "task_id": getattr(event, "task_id", None),
            }
            for seq, event in pairs
        ]

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"description": "Idempotent replay (Idempotency-Key 重试命中已创建 Run)"},
        },
    )
    async def create_run(
        request: CreateRunRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        idempotency_key: Annotated[
            str | None,
            Header(min_length=1, pattern=r"\S+", alias="Idempotency-Key"),
        ] = None,
    ) -> JSONResponse:
        context = _tenant(actor, request.workspace_id)
        sessions = sessions_factory(actor, context.workspace_id)
        # body workspace 是授权事实的「声明」：先验证归属（跨 org/不存在统一 404
        # 防枚举），再做成员校验（org 内无资格 403）——顺序不可倒置，否则跨 org
        # 探测能区分「存在但无权」与「不存在」。
        from sqlalchemy import select

        from zhiwei.persistence.models import Workspace

        async with tenant_session(sessions, context) as session:
            workspace_exists = await session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == context.organization_id,
                    Workspace.id == context.workspace_id,
                )
            )
        if workspace_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
            )
        try:
            await workspace_authorizer(actor, request.workspace_id)
        except MembershipScopeError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="outside workspace scope",
            ) from exc
        try:
            planned = planner.plan(PlanIntent(template=request.template))
        except PlannerError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        request_digest = (
            canonical_request_digest(
                "POST", request_scope.url.path, request.model_dump(mode="json")
            )
            if idempotency_key is not None
            else None
        )
        run_id, replay = await _submit_run(
            sessions, context, planned, actor, request.template,
            idempotency_key=idempotency_key, request_digest=request_digest,
        )
        if replay is not None:
            # 幂等重放（F-R5-05）：返回原 run 的存储响应，不重复提交不重复
            # dispatch——重试语义与 organizations/workspaces 同型（200）。
            return JSONResponse(
                content=replay, status_code=status.HTTP_200_OK
            )
        await _dispatch(sessions, context)
        return JSONResponse(
            content={
                "run_id": str(run_id),
                "status": "created",
                "template": request.template,
            },
            status_code=status.HTTP_201_CREATED,
        )

    @router.get("/{run_id}/approvals", response_model=list[ApprovalRequestView])
    async def list_approvals(
        request_scope: Request,
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ApprovalRequestView]:
        context = _tenant(actor)
        await _authorize_run_read(request_scope, actor, context, run_id)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            requests = await store.list_for_run(run_id)
        return [
            ApprovalRequestView(
                request_id=r.request_id,
                run_id=r.run_id,
                task_id=r.task_id,
                status=r.status,
                requester=r.requester,
            )
            for r in requests
        ]

    @router.post(
        "/{run_id}/approvals/{request_id}/decision",
        response_model=DecisionResult,
    )
    async def decide_approval(
        request_scope: Request,
        run_id: UUID,
        request_id: UUID,
        request: ApprovalDecisionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> DecisionResult:
        """审批决策：归属校验前置 + 决策与信号同事务提交（H-3，spec §4 增补）。

        决策落账与 approval_decided 信号的 outbox 行在同一事务：信号入列失败
        时决策一并回滚（全有或全无）——两事务分离会在崩溃窗口留下「决策已
        生效但 workflow 永远等不到信号」的挂起面（ADR-012 反例）。

        PEP（F-R3-02/F-R2-13）：非 USER principal 显式拒绝（denied 审计）；
        tool_approval.approve/reject cell 求值先于业务事务，当事人证据从
        approval 权威记录解析（_approval_evidence）；决策写 allowed（同事务，
        与决策+信号同回滚）/ failed（SoD 等业务拒绝，独立事务）审计。
        """
        context = _tenant(actor)
        if request.decision not in {"approved", "rejected"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="decision must be 'approved' or 'rejected'",
            )
        if actor.kind is not PrincipalKind.USER:
            # F-R2-13：审批决策是人类职责。S1 会话路径只产生 USER（结构性防
            # 线，identity/sessions）；本断言是对未来 service-account/token
            # 认证路径的显式报警点，拒绝写 denied 审计（独立事务）。
            audit_request_id, trace_id = request_trace(request_scope)
            denial = policy_enforcer.deny("non_human_principal")
            await append_fail_closed_audit(
                sessions_factory(actor, context.workspace_id),
                context,
                denied_audit_record(
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_DECIDE,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=request_id,
                    decision=denial,
                    reason="non_human_principal",
                    request_id=audit_request_id,
                    trace_id=trace_id,
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="human principal required",
            )
        approver = str(actor.principal_id)
        sessions = sessions_factory(actor, context.workspace_id)
        # 归属校验先于决策：错 run_id 的请求不得污染目标审批（此前依赖
        # 异常隐式回滚兜底——显式化，防未来重构破坏该不变量）。未知/跨
        # 租户 request 与「不属于本 run」同语义 404（防枚举）。
        async with tenant_session(sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            try:
                record = await store.get(request_id)
            except ApprovalError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="approval request not found",
                ) from exc
            if record.run_id != run_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="approval request does not belong to this run",
                )
        # PEP 先于业务事务（与 releases/discover 同序）：证据取自权威记录，
        # deny → 独立事务 denied 审计 + 403，决策零写入。
        audit_request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action=_AUDIT_ACTION_DECIDE,
            resource_type=_AUDIT_RESOURCE_TYPE,
            policy_type=ResourceType.TOOL_APPROVAL,
            policy_action=Action.APPROVE if request.decision == "approved" else Action.REJECT,
            resource_id=request_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=audit_request_id,
            trace_id=trace_id,
            resource_context=_approval_evidence(record),
        )
        async with tenant_session(sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            try:
                decided = await store.decide(
                    request_id=request_id,
                    decision=request.decision,
                    approver=approver,
                    reason=request.reason,
                )
            except ApprovalError as exc:
                # PEP 放行后的业务拒绝（store 层 SoD/CAS/过期）：failed 审计
                # 独立事务，409 机器可读拒绝面
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action=_AUDIT_ACTION_DECIDE,
                    resource_type=_AUDIT_RESOURCE_TYPE,
                    resource_id=request_id,
                    error=exc,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action=_AUDIT_ACTION_DECIDE,
                resource_type=_AUDIT_RESOURCE_TYPE,
                resource_id=request_id,
                resource_version=1,
                authorization=authorization,
            )
            # 决策 + 信号 outbox 行：同一事务（上方 tenant_session 块内）
            from zhiwei.contracts.identifiers import new_id
            from zhiwei.contracts.time import utc_now
            from zhiwei.persistence.models import OutboxMessage
            from zhiwei.runtime.commands import SignalRun

            command = SignalRun(
                run_id=run_id,
                signal_name="approval_decided",
                payload={
                    "command_event_id": str(new_id()),
                    "task_id": decided.task_id,
                    "decision": request.decision,
                },
            )
            now = utc_now()
            session.add(
                OutboxMessage(
                    id=uuid4(),
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    topic="runtime.command",
                    event_key=command.kind.value,
                    payload=command.model_dump(mode="json"),
                    status="pending",
                    attempts=0,
                    available_at=now,
                    schema_version=1,
                    created_at=now,
                )
            )
        await _dispatch(sessions, context)
        return DecisionResult(
            request_id=request_id,
            decision=request.decision,
            accepted=True,
        )

    async def _submit_run(
        session_factory,
        context: TenantContext,
        planned: Any,
        actor: ActorContext,
        template: str | None,
        *,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
    ) -> tuple[UUID, dict[str, Any] | None]:
        from uuid import uuid4

        run_id = uuid4()
        async with tenant_session(session_factory, context) as session:
            if idempotency_key is not None and request_digest is not None:
                # F-R5-05：claim 与 Run 行 + outbox 同事务——提交前进程死亡
                # 回滚重 claim，提交后重试命中 claim 返回原 run_id。
                repo = TenantRepository(session, context)
                try:
                    claim = await repo.claim_idempotency(
                        scope=IDEMPOTENCY_SCOPE_RUN_START,
                        key=idempotency_key,
                        request_digest=request_digest,
                        response={
                            "run_id": str(run_id),
                            "status": "created",
                            "template": template,
                        },
                    )
                except IdempotencyConflict as exc:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="idempotency key was already used for another request",
                    ) from exc
                if not claim.created:
                    return run_id, claim.response
            service = RunCommandService(session, context)
            try:
                await service.submit_start_run(
                    run_id=run_id,
                    graph=planned.graph.model_dump(mode="json"),
                    # pack 计划源可 pin 执行队列（pack fixture 绑定的执行面）；
                    # None → router 默认队列（S2 fixture 模板语义不变）
                    task_queue=planned.task_queue or task_queue,
                    max_task_attempts=planned.max_task_attempts,
                    continue_as_new_after=planned.continue_as_new_after,
                    # SoD 事实源：审批 requester 从 API actor 穿透（ADR-012 反例 1）
                    requested_by=str(actor.principal_id),
                    # 创建期 caller-declared 绑定持久化（S10 fix-A，0019）
                    template=template,
                )
            except RunCommandError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
                ) from exc
        return run_id, None

    return router


def timedelta_safe() -> Any:
    from datetime import timedelta

    return timedelta(milliseconds=50)
