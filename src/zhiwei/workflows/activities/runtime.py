"""S2 runtime: Temporal activities — the only side-effect boundary of the run。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan。

每个 activity 打开自己的 tenant 事务：RuntimeEventStore → CanonicalUnitOfWork。幂等键
由调用方（workflow）按逻辑身份派生，activity 重试时先查 `has_event` 再落账，同键只
会有一次写入；同键不同内容由 UoW 以 EventIdempotencyConflict 拒绝。

handler 业务失败（抛异常）= TaskFailed 终态，正常返回（不抛）给 Temporal，避免对
确定性业务失败做无意义重试；基础设施失败（DB 等）向上抛，交给 Temporal retry policy。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from zhiwei.agents.task_graph import TaskGraph
from zhiwei.contracts.canonical import digest_bytes
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.approvals import ApprovalRequestStore
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptCreated,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunStarted,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workflows.activities.base import (
    ActivityEventAck,
    CreateApprovalInput,
    ExecuteTaskInput,
    RecordApprovalOutcomeInput,
    RecordRunTerminalInput,
    RecordTaskFailedInput,
    RecordTaskSkippedInput,
    StartRunActivityInput,
    TaskExecutionResult,
    approval_outcome_key,
    attempt_key,
    attempt_terminal_key,
    run_created_key,
    run_started_key,
    run_terminal_key,
    scheduled_key,
    started_key,
    task_skipped_key,
    terminal_key,
)

logger = logging.getLogger(__name__)


def _context(organization_id: str, workspace_id: str) -> TenantContext:
    return TenantContext(
        organization_id=UUID(organization_id), workspace_id=UUID(workspace_id)
    )


class RuntimeActivities:
    """Activity implementations bound to PG canonical events and task handlers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handler_registry: TaskHandlerRegistry,
    ) -> None:
        self._sessions = session_factory
        self._handlers = handler_registry

    @activity.defn
    async def start_run(self, input: StartRunActivityInput) -> ActivityEventAck:
        run_id = UUID(input.run_id)
        graph = TaskGraph.model_validate(input.graph)
        context = _context(input.organization_id, input.workspace_id)
        created = 0
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if not await store.has_event(run_id, run_created_key(input.run_id)):
                await store.append(
                    RunCreated(
                        run_id=run_id,
                        timestamp=utc_now(),
                        graph=graph,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=run_created_key(input.run_id),
                )
                created += 1
            if not await store.has_event(run_id, run_started_key(input.run_id)):
                await store.append(
                    RunStarted(
                        run_id=run_id, timestamp=utc_now()
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=run_started_key(input.run_id),
                )
                created += 1
        return ActivityEventAck(run_id=input.run_id, created_events=created)

    @activity.defn
    async def execute_task(self, input: ExecuteTaskInput) -> TaskExecutionResult:
        run_id = UUID(input.run_id)
        attempt_id = UUID(input.attempt_id)
        context = _context(input.organization_id, input.workspace_id)
        now = utc_now()

        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if not await store.has_event(run_id, scheduled_key(input.run_id, input.task_id)):
                await store.append(
                    TaskScheduled(run_id=run_id, timestamp=now, task_id=input.task_id),
                    actor_ref=input.actor_ref,
                    idempotency_key=scheduled_key(input.run_id, input.task_id),
                )
            if not await store.has_event(run_id, attempt_key(input.run_id, input.task_id, input.attempt_no)):
                await store.append(
                    AttemptCreated(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                        attempt_number=input.attempt_no,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=attempt_key(input.run_id, input.task_id, input.attempt_no),
                )
            if not await store.has_event(run_id, started_key(input.run_id, input.task_id, input.attempt_no)):
                await store.append(
                    TaskStarted(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=started_key(input.run_id, input.task_id, input.attempt_no),
                )

            # handler 在事务外执行：业务执行不是数据库事务的一部分；结果事件在
            # 第二个事务落账。fixture handler 纯函数，重试安全；S3+ 的真实副作用
            # handler 由 ActionReceipt 幂等（S2-T5 语义）。
        handler = self._handlers.get(input.task_type, input.handler_version)
        handler_input = TaskInput(
            task_id=input.task_id,
            attempt_id=attempt_id,
            input_values=input.input_values,
        )
        try:
            handler.validate_input(handler_input)
            # sync handler 放线程池：S3+ 的真实 IO handler 不能阻塞 worker 事件循环
            output = await asyncio.to_thread(handler.execute, handler_input)
            handler.validate_output(output)
        except Exception as exc:
            async with tenant_session(self._sessions, context) as session:
                store = RuntimeEventStore(session, context)
                if not await store.has_event(
                    run_id, terminal_key(input.run_id, input.task_id, input.attempt_no)
                ):
                    await store.append(
                        TaskFailed(
                            run_id=run_id,
                            timestamp=utc_now(),
                            task_id=input.task_id,
                            error=str(exc),
                            attempt_id=attempt_id,
                        ),
                        actor_ref=input.actor_ref,
                        idempotency_key=terminal_key(
                            input.run_id, input.task_id, input.attempt_no
                        ),
                    )
                if not await store.has_event(
                    run_id,
                    attempt_terminal_key(input.run_id, input.task_id, input.attempt_no),
                ):
                    await store.append(
                        AttemptAborted(
                            run_id=run_id,
                            timestamp=utc_now(),
                            task_id=input.task_id,
                            attempt_id=attempt_id,
                        ),
                        actor_ref=input.actor_ref,
                        idempotency_key=attempt_terminal_key(
                            input.run_id, input.task_id, input.attempt_no
                        ),
                    )
            return TaskExecutionResult(
                task_id=input.task_id,
                status="failed",
                attempt_no=input.attempt_no,
                error=str(exc),
            )

        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if not await store.has_event(
                run_id, terminal_key(input.run_id, input.task_id, input.attempt_no)
            ):
                await store.append(
                    TaskCompleted(
                        run_id=run_id,
                        timestamp=utc_now(),
                        task_id=input.task_id,
                        output_values=dict(output.output_values),
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=terminal_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
            if not await store.has_event(
                run_id,
                attempt_terminal_key(input.run_id, input.task_id, input.attempt_no),
            ):
                await store.append(
                    AttemptCommitted(
                        run_id=run_id,
                        timestamp=utc_now(),
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=attempt_terminal_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
        return TaskExecutionResult(
            task_id=input.task_id,
            status="completed",
            attempt_no=input.attempt_no,
            output_values=dict(output.output_values),
        )

    @activity.defn
    async def create_approval(self, input: CreateApprovalInput) -> ActivityEventAck:
        """为 RequestApproval 任务创建 PG 审批请求（幂等：任务级唯一键）。"""
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        async with tenant_session(self._sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            existing = await store.list_for_run(run_id)
            if any(r.task_id == input.task_id for r in existing):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
            # exact input digest：S2 fixture 的任务输入即 task_id 绑定的确定值；
            # digest 绑定沿用域语义（swap 检测在域层 CAS/唯一键处收口）
            await store.create(
                run_id=run_id,
                task_id=input.task_id,
                input_digest=digest_bytes(
                    f"approval:{input.run_id}:{input.task_id}".encode()
                ),
                requester="agent-runtime",
                agent_identity="agent-runtime:fixture",
                requested_by=input.requested_by,
            )
        return ActivityEventAck(run_id=input.run_id, created_events=1)

    @activity.defn
    async def record_approval_outcome(
        self, input: RecordApprovalOutcomeInput
    ) -> TaskExecutionResult:
        """审批决策落账为任务终态事件（幂等键含 attempt_no）。"""
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        now = utc_now()
        key = approval_outcome_key(input.run_id, input.task_id, input.attempt_no)
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if await store.has_event(run_id, key):
                return TaskExecutionResult(
                    task_id=input.task_id,
                    status="completed" if input.decision == "approved" else "failed",
                    attempt_no=input.attempt_no,
                )
            # 审批任务走专用等待路径，未经 execute_task 的普通事件序；决策落账时
            # 补全调度/尝试生命周期（reducer 状态机要求 pending→scheduled→started
            # →terminal，审批任务不豁免）。
            if not await store.has_event(
                run_id, scheduled_key(input.run_id, input.task_id)
            ):
                await store.append(
                    TaskScheduled(run_id=run_id, timestamp=now, task_id=input.task_id),
                    actor_ref=input.actor_ref,
                    idempotency_key=scheduled_key(input.run_id, input.task_id),
                )
            attempt_id = _deterministic_attempt_id(
                input.run_id, input.task_id, input.attempt_no
            )
            if not await store.has_event(
                run_id, attempt_key(input.run_id, input.task_id, input.attempt_no)
            ):
                await store.append(
                    AttemptCreated(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                        attempt_number=input.attempt_no,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=attempt_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
            if not await store.has_event(
                run_id, started_key(input.run_id, input.task_id, input.attempt_no)
            ):
                await store.append(
                    TaskStarted(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=started_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
            if input.decision == "approved":
                await store.append(
                    TaskCompleted(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        output_values={"approval": "approved"},
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=key,
                )
            else:
                await store.append(
                    TaskFailed(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        error="rejected by approver",
                        attempt_id=attempt_id,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=key,
                )
            if not await store.has_event(
                run_id,
                attempt_terminal_key(input.run_id, input.task_id, input.attempt_no),
            ):
                await store.append(
                    AttemptCommitted(run_id=run_id, timestamp=now, task_id=input.task_id, attempt_id=attempt_id)
                    if input.decision == "approved"
                    else AttemptAborted(run_id=run_id, timestamp=now, task_id=input.task_id, attempt_id=attempt_id),
                    actor_ref=input.actor_ref,
                    idempotency_key=attempt_terminal_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
        return TaskExecutionResult(
            task_id=input.task_id,
            status="completed" if input.decision == "approved" else "failed",
            attempt_no=input.attempt_no,
        )

    @activity.defn
    async def record_run_terminal(
        self, input: RecordRunTerminalInput
    ) -> ActivityEventAck:
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        now = utc_now()
        key = run_terminal_key(input.run_id, input.outcome)
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if await store.has_event(run_id, key):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
            if input.outcome == "completed":
                event: Any = RunCompleted(run_id=run_id, timestamp=now)
            elif input.outcome == "failed":
                event = RunFailed(run_id=run_id, timestamp=now, error=input.error or "unknown")
            elif input.outcome == "cancelled":
                event = RunCancelled(run_id=run_id, timestamp=now, reason=input.reason)
            else:
                raise ValueError(f"unknown run outcome: {input.outcome!r}")
            await store.append(event, actor_ref=input.actor_ref, idempotency_key=key)
        return ActivityEventAck(run_id=input.run_id, created_events=1)

    @activity.defn
    async def record_task_skipped(
        self, input: RecordTaskSkippedInput
    ) -> ActivityEventAck:
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        key = task_skipped_key(input.run_id, input.task_id)
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if await store.has_event(run_id, key):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
            await store.append(
                TaskSkipped(
                    run_id=run_id,
                    timestamp=utc_now(),
                    task_id=input.task_id,
                    reason=input.reason,
                ),
                actor_ref=input.actor_ref,
                idempotency_key=key,
            )
        return ActivityEventAck(run_id=input.run_id, created_events=1)

    @activity.defn
    async def record_task_failed(
        self, input: RecordTaskFailedInput
    ) -> ActivityEventAck:
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        key = terminal_key(input.run_id, input.task_id, input.attempt_no)
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if await store.has_event(run_id, key):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
            await store.append(
                TaskFailed(
                    run_id=run_id,
                    timestamp=utc_now(),
                    task_id=input.task_id,
                    error=input.error,
                ),
                actor_ref=input.actor_ref,
                idempotency_key=key,
            )
        return ActivityEventAck(run_id=input.run_id, created_events=1)


def _deterministic_attempt_id(run_id: str, task_id: str, attempt_no: int) -> UUID:
    """审批路径的 attempt id 从逻辑身份派生（重试重放同 id）。"""
    import uuid as uuid_module

    return uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"{run_id}:{task_id}:{attempt_no}")
