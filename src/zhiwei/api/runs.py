"""S2-T7：Run API——REST 投影绑定 PG 真相 + Planner port + 审批决策端点。

事实源：specs/s2-agent-runtime.md §3/§5（REST projection 可恢复；sandbox run）。

- GET /runs / GET /runs/{id} / GET /runs/{id}/events：从 PG canonical events
  reduce（刷新/断网恢复的权威来源），无进程内缓存；
- POST /runs：经 Planner port 产出图 → RunCommandService（Run 行 + outbox 命令
  同事务）→ 请求内联 dispatch（S2 单进程形态；多租户后台 dispatcher 属 S11）；
- POST /runs/{id}/approvals/{request_id}/decision：ApprovalRequestStore 的 CAS +
  SoD 守护，决策经命令路径投递给 workflow（approval_decided 信号）。
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.approvals import ApprovalRequestStore
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.approvals import ApprovalError
from zhiwei.runtime.planner import FixturePlanner, PlanIntent, Planner, PlannerError
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE
from zhiwei.workers.outbox_dispatcher import OutboxDispatcher
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

logger = logging.getLogger(__name__)


class RunRecord(BaseModel):
    """一个 run 的列表项投影（来自 PG reduce）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    organization_id: UUID


class RunDetail(BaseModel):
    """run 详情（含任务投影）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    organization_id: UUID
    tasks: dict[str, dict[str, Any]] = {}


class CreateRunRequest(BaseModel):
    """POST /runs 的请求体（planner 意图）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: str = "single-fixture"
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


def create_runs_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions_factory: Callable[[ActorContext, UUID | None], async_sessionmaker[AsyncSession]],
    temporal_target: str,
    planner: Planner | None = None,
    dispatch_inline: bool = True,
    task_queue: str = DEFAULT_TASK_QUEUE,
) -> APIRouter:
    """Run API router。

    sessions_factory：actor+workspace → PG session factory（app 组装期绑定）；
    temporal_target：Temporal 前端地址（local-product 默认 dev server）；
    dispatch_inline：命令提交后在同一请求内跑一轮 dispatcher poll（S2 单进程
    形态，见 docstring）。
    """
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

    @router.get("", response_model=list[RunRecord])
    async def list_runs(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[RunRecord]:
        context = _tenant(actor)
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
                    )
                )
        return records

    @router.get("/{run_id}", response_model=RunDetail)
    async def get_run(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> RunDetail:
        context = _tenant(actor)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(run_id)
            if state.graph is None and state.status == "created":
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
        )

    @router.get("/{run_id}/events")
    async def get_run_events(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[dict[str, Any]]:
        context = _tenant(actor)
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

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_run(
        request: CreateRunRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> dict[str, Any]:
        context = _tenant(actor, request.workspace_id)
        try:
            planned = planner.plan(PlanIntent(template=request.template))
        except PlannerError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        sessions = sessions_factory(actor, context.workspace_id)
        run_id = await _submit_run(sessions, context, planned, actor)
        await _dispatch(sessions, context)
        return {
            "run_id": str(run_id),
            "status": "created",
            "template": request.template,
        }

    @router.get("/{run_id}/approvals", response_model=list[ApprovalRequestView])
    async def list_approvals(
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[ApprovalRequestView]:
        context = _tenant(actor)
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
        run_id: UUID,
        request_id: UUID,
        request: ApprovalDecisionRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> DecisionResult:
        context = _tenant(actor)
        if request.decision not in {"approved", "rejected"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="decision must be 'approved' or 'rejected'",
            )
        approver = str(actor.principal_id)
        sessions = sessions_factory(actor, context.workspace_id)
        async with tenant_session(sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            try:
                record = await store.decide(
                    request_id=request_id,
                    decision=request.decision,
                    approver=approver,
                    reason=request.reason,
                )
            except ApprovalError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            if record.run_id != run_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="approval request does not belong to this run",
                )
        # 决策经生产命令路径投递给 workflow（cancel 同款信号通道）
        async with tenant_session(sessions, context) as session:
            from uuid import uuid4

            from zhiwei.contracts.identifiers import new_id
            from zhiwei.contracts.time import utc_now
            from zhiwei.persistence.models import OutboxMessage
            from zhiwei.runtime.commands import SignalRun

            command = SignalRun(
                run_id=run_id,
                signal_name="approval_decided",
                payload={
                    "command_event_id": str(new_id()),
                    "task_id": record.task_id,
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
    ) -> UUID:
        from uuid import uuid4

        run_id = uuid4()
        async with tenant_session(session_factory, context) as session:
            service = RunCommandService(session, context)
            await service.submit_start_run(
                run_id=run_id,
                graph=planned.graph.model_dump(mode="json"),
                task_queue=task_queue,
                max_task_attempts=planned.max_task_attempts,
                continue_as_new_after=planned.continue_as_new_after,
            )
        return run_id

    return router


def timedelta_safe() -> Any:
    from datetime import timedelta

    return timedelta(milliseconds=50)
