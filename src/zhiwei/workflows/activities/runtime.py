"""S2 runtime: Temporal activities — the only side-effect boundary of the run。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan。

每个 activity 打开自己的 tenant 事务：RuntimeEventStore → CanonicalUnitOfWork。幂等键
由调用方（workflow）按逻辑身份派生，activity 重试时先查 `has_event` 再落账，同键只
会有一次写入；同键不同内容由 UoW 以 EventIdempotencyConflict 拒绝。

handler 业务失败（抛异常）= TaskFailed 终态，正常返回（不抛）给 Temporal，避免对
确定性业务失败做无意义重试；基础设施失败（DB 等）向上抛，交给 Temporal retry policy。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from zhiwei.agents.task_graph import TaskGraph
from zhiwei.contracts.time import utc_now
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
from zhiwei.runtime.persistence import RuntimeEventStore
from zhiwei.workflows.activities.base import (
    ActivityEventAck,
    ExecuteTaskInput,
    RecordRunTerminalInput,
    RecordTaskFailedInput,
    RecordTaskSkippedInput,
    StartRunActivityInput,
    TaskExecutionResult,
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
            output = handler.execute(handler_input)
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
