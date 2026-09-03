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
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.approvals import ApprovalRequestStore
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptCreated,
    ConflictDetected,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.handlers.base import EffectUnknownError, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workflows.activities.base import (
    ActivityEventAck,
    CheckApprovalInput,
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
        # registry 预检（spec §3「validate 阶段检查完整性」）：missing handler
        # 在 Run 事件落账前失败——不产生任何 canonical event（fail closed）。
        # TaskHandlerRegistryError 在 workflow 重试策略中列为 non-retryable。
        # RequestApproval 例外：它走专用等待路径（create_approval/决策信号/
        # record_approval_outcome），不经 handler 执行——要求注册 handler 没有
        # 语义，只会迫使无意义注册。
        self._handlers.validate_completeness(
            {
                node.task_type
                for node in graph.nodes.values()
                if node.task_type != "RequestApproval"
            }
        )
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
        # Synthesize 降级门（ADR-005 硬约束 2）：存在未解决 conflict 时不得
        # 产出正常合成输出——降级为标记输出（S2 无 Fact/Inference 词表，
        # 门的形式是「结构性地不产出正常输出」）。
        downgraded_for_conflicts = False
        conflict_fields_to_report: list[str] = []
        if input.task_type == "Synthesize":
            async with tenant_session(self._sessions, context) as session:
                state = await RuntimeEventStore(session, context).reduce_state(run_id)
            if state.conflicts:
                downgraded_for_conflicts = True
                conflict_fields_to_report = sorted(
                    {c.field for c in state.conflicts}
                )

        handler = self._handlers.get(input.task_type, input.handler_version)
        handler_input = TaskInput(
            task_id=input.task_id,
            attempt_id=attempt_id,
            input_values=input.input_values,
        )
        if downgraded_for_conflicts:
            output = TaskOutput(
                output_values={
                    "synthesize_downgraded": True,
                    "unresolved_conflict_fields": conflict_fields_to_report,
                }
            )
        else:
            try:
                handler.validate_input(handler_input)
                # sync handler 放线程池：S3+ 的真实 IO handler 不能阻塞 worker 事件循环
                output = await asyncio.to_thread(handler.execute, handler_input)
                handler.validate_output(output)
            except EffectUnknownError:
                # effect_unknown：副作用可能已发生，禁自动重试（spec §4 增补）。
                # 前置生命周期事件已在第一事务落账（scheduled/attempt/started），
                # 此处落 TaskFailed + AttemptAborted，返回 effect_unknown 状态供
                # workflow 侧的重试门消费。
                async with tenant_session(self._sessions, context) as session:
                    store = RuntimeEventStore(session, context)
                    await self._append_failure_events(
                        store, input, run_id, attempt_id,
                        error=f"effect state unknown: {input.task_id}",
                    )
                return TaskExecutionResult(
                    task_id=input.task_id,
                    status="effect_unknown",
                    attempt_no=input.attempt_no,
                    error="effect state unknown",
                )
            except Exception as exc:
                async with tenant_session(self._sessions, context) as session:
                    store = RuntimeEventStore(session, context)
                    await self._append_failure_events(
                        store, input, run_id, attempt_id, error=str(exc)
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
                # 冲突落账（spec §3 增补 / ADR-005 增补 4）：conflict_preserving
                # 字段存在 prior 值时，TaskCompleted 之外追加 ConflictDetected
                # canonical event（重放后冲突证据仍在，不只存在于内存投影）。
                # prior 值/写者在追加前从当前事件序列 reduce 得出；reduce 必须
                # 在 run advisory lock 内——并发完成事务串行化后，后落账者才能
                # 确定性地看到先落账者的 TaskCompleted（读已提交语义）。
                state_before = None
                if input.conflict_fields:
                    await store.lock_run(run_id)
                    state_before = await store.reduce_state(run_id)
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
                if state_before is not None:
                    await self._append_conflict_events(
                        store, input, state_before, dict(output.output_values)
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

    async def _append_failure_events(
        self,
        store: RuntimeEventStore,
        input: ExecuteTaskInput,
        run_id: UUID,
        attempt_id: UUID,
        *,
        error: str,
    ) -> None:
        """execute_task 失败路径的终态落账（TaskFailed + AttemptAborted，幂等）。"""
        if not await store.has_event(
            run_id, terminal_key(input.run_id, input.task_id, input.attempt_no)
        ):
            await store.append(
                TaskFailed(
                    run_id=run_id,
                    timestamp=utc_now(),
                    task_id=input.task_id,
                    error=error,
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

    @staticmethod
    async def _append_conflict_events(
        store: RuntimeEventStore,
        input: ExecuteTaskInput,
        state_before: Any,
        output_values: dict[str, Any],
    ) -> None:
        """conflict_preserving 字段的冲突落账（双方 task/value + attempt 证据）。"""
        run_id = UUID(input.run_id)
        for field_name in sorted(input.conflict_fields):
            if field_name not in output_values:
                continue
            prior = state_before.canonical.get(field_name)
            if prior is None:
                continue
            prev_task = state_before.field_owners.get(field_name)
            if prev_task is None or prev_task == input.task_id:
                continue
            values = {prev_task: prior, input.task_id: output_values[field_name]}
            conflict_record = {
                "values": values,
                "attempt_values": {input.task_id: str(input.attempt_id)},
                "evidence_refs": [],
            }
            await store.append(
                ConflictDetected(
                    run_id=run_id,
                    timestamp=utc_now(),
                    field=field_name,
                    values=values,
                    conflict_record=conflict_record,
                ),
                actor_ref=input.actor_ref,
                idempotency_key=(
                    f"conflict:{input.run_id}:{field_name}:{prev_task}:{input.task_id}"
                ),
            )

    @activity.defn
    async def create_approval(self, input: CreateApprovalInput) -> ActivityEventAck:
        """为 RequestApproval 任务创建 PG 审批请求（幂等：任务级唯一键）。

        requester 承载触发 run 的 human principal（H-1：经 StartRun 命令从
        POST /runs 的 actor 穿透）——SoD 三层防御（域层/PG store/DB CHECK）
        比较的都是本列；退化为常量会使三层同时失效（ADR-012 反例 1）。
        requested_by 同值冗余存储（0011 列语义：发起人可读标识）。
        """
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        async with tenant_session(self._sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            existing = await store.list_for_run(run_id)
            if any(r.task_id == input.task_id for r in existing):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
            requester = input.requested_by or "system"
            # expiry 必设（spec §4 2026-09-03 增补）：pending 审批的等待上界，
            # 过期由 store.decide 拒绝 + workflow 等待超时双路解除
            from datetime import timedelta

            expires_at = utc_now() + timedelta(seconds=input.approval_expiry_seconds)
            # digest 绑定审批节点的声明内容（spec §4 增补）：身份常量派生使
            # swap 检测结构上不可触发——节点契约变化必须产生新 digest/新请求
            input_digest = (
                digest_bytes(canonical_json(input.node_content))
                if input.node_content
                else digest_bytes(
                    f"approval:{input.run_id}:{input.task_id}".encode()
                )
            )
            await store.create(
                run_id=run_id,
                task_id=input.task_id,
                input_digest=input_digest,
                requester=requester,
                agent_identity="agent-runtime:fixture",
                requested_by=requester,
                expires_at=expires_at,
            )
        return ActivityEventAck(run_id=input.run_id, created_events=1)

    @activity.defn
    async def check_approval(self, input: CheckApprovalInput) -> dict[str, Any]:
        """expired 路径的权威行回查：决策已落账但信号迟到时不误判 expired。

        只读 PG 审批行（真相层），返回 {decision: approved|rejected|pending}。
        """
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        async with tenant_session(self._sessions, context) as session:
            store = ApprovalRequestStore(session, context)
            requests = await store.list_for_run(run_id)
        for record in requests:
            if record.task_id == input.task_id:
                if record.status in {"approved", "rejected"}:
                    return {"decision": record.status}
                return {"decision": "pending"}
        return {"decision": "pending"}

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
                # rejected（审批人拒绝）或 expired（等待上界到期，spec §4 增补）
                error = (
                    "rejected by approver"
                    if input.decision == "rejected"
                    else "approval expired"
                )
                await store.append(
                    TaskFailed(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        error=error,
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
            elif input.outcome == "paused":
                # pause/resume 落账（spec §3/§4 增补，ADR-012 反例）：PG 真相
                # 必须反映暂停态——否则 REST/SSE 投影在暂停期间恒显 running
                event = RunPaused(run_id=run_id, timestamp=now)
            elif input.outcome == "resumed":
                event = RunResumed(run_id=run_id, timestamp=now)
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
        """ActivityError 路径的 TaskFailed 终态落账（幂等键含 attempt_no）。

        前置生命周期补全（C-1）：execute_task 的第一个事务（scheduled/attempt/
        started）可能因基础设施故障重试耗尽而从未提交——此时直接落 TaskFailed 会
        产生 reducer 拒绝的序列（pending→failed 非法），该 run 的每次 reduce 都
        崩溃且无自愈。与 record_approval_outcome 相同的 backfill 模式：先补全
        pending→scheduled→started，再落终态。
        """
        run_id = UUID(input.run_id)
        context = _context(input.organization_id, input.workspace_id)
        now = utc_now()
        key = terminal_key(input.run_id, input.task_id, input.attempt_no)
        async with tenant_session(self._sessions, context) as session:
            store = RuntimeEventStore(session, context)
            if await store.has_event(run_id, key):
                return ActivityEventAck(run_id=input.run_id, created_events=0)
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
            await store.append(
                TaskFailed(
                    run_id=run_id,
                    timestamp=now,
                    task_id=input.task_id,
                    error=input.error,
                    attempt_id=attempt_id,
                ),
                actor_ref=input.actor_ref,
                idempotency_key=key,
            )
            # attempt 级终态对称（D-3）：execute_task 失败路径与审批路径都落
            # AttemptAborted/AttemptCommitted——缺它时 attempt 投影停留 pending
            if not await store.has_event(
                run_id,
                attempt_terminal_key(input.run_id, input.task_id, input.attempt_no),
            ):
                await store.append(
                    AttemptAborted(
                        run_id=run_id,
                        timestamp=now,
                        task_id=input.task_id,
                        attempt_id=attempt_id,
                    ),
                    actor_ref=input.actor_ref,
                    idempotency_key=attempt_terminal_key(
                        input.run_id, input.task_id, input.attempt_no
                    ),
                )
        return ActivityEventAck(run_id=input.run_id, created_events=1)


def _deterministic_attempt_id(run_id: str, task_id: str, attempt_no: int) -> UUID:
    """审批路径的 attempt id 从逻辑身份派生（重试重放同 id）。"""
    import uuid as uuid_module

    return uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"{run_id}:{task_id}:{attempt_no}")
