"""S2 runtime: Temporal workflow for agent runs（deterministic orchestration only）。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan、总设计 §4.3。

Workflow 只推进执行位置：排序、并行分派、信号、重试位置、Continue-As-New。全部编排
决策派生自「图 + 已记录的 activity 结果」（replay 确定）；PG canonical events 是唯一
业务真相，workflow 不持有权威状态。大 payload 不进 history——图是小的类型化元数据，
其余输入只携带 refs（run/org/ws id）。

确定性纪律：不使用 wall clock / uuid4 / random；attempt_id 用 workflow.uuid4()，
时间戳全部由 activity 落账。pydantic 校验（TaskGraph）在 workflow 内是纯函数，
经 imports_passed_through 显式放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from zhiwei.workflows.activities.base import (
    CreateApprovalInput,
    ExecuteTaskInput,
    RecordApprovalOutcomeInput,
    RecordRunTerminalInput,
    RecordTaskFailedInput,
    RecordTaskSkippedInput,
    StartRunActivityInput,
)

with workflow.unsafe.imports_passed_through():
    from zhiwei.agents.task_graph import FailurePolicy, TaskGraph

# 基础设施失败（DB 抖动等）重试；确定性业务拒绝（Run 缺失、schema 未知）不
# 重试——重试同样的输入只会得到同样的拒绝。错误类型名是异常类裸名
# （temporalio 默认 failure converter 以 __class__.__name__ 上报）。
# EventIdempotencyConflict 不列入：并发重复执行下同键不同时间戳的冲突是良性
# 竞态，重试后 has_event 命中已提交行即收敛（改为 non-retryable 会把健康
# task 误判失败）。
_INFRA_RETRY_POLICY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(milliseconds=200),
    maximum_interval=timedelta(seconds=2),
    non_retryable_error_types=[
        "RunNotFound",
        "RuntimeEventSchemaError",
        "ValueError",
    ],
)


@dataclass
class AgentRunWorkflowInput:
    """Workflow input; carries only refs plus the small typed graph metadata.

    continue-as-new 携带状态：已终态 task 集合与已派发计数（history 不重复存储大状态）。
    """

    run_id: str
    organization_id: str
    workspace_id: str
    graph: dict[str, Any]
    task_queue: str
    max_task_attempts: int = 3
    continue_as_new_after: int = 1000
    activity_timeout_seconds: int = 60
    actor_ref: str = "agent-runtime:worker"
    # continue-as-new carried state（含信号状态：cancel/pause 意图与已见命令 id
    # 必须跨 CAN 存活，否则 CANCEL policy/operator cancel 在 CAN 边界被静默吞掉）
    started: bool = False
    completed_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    skipped_tasks: list[str] = field(default_factory=list)
    attempt_counts: dict[str, int] = field(default_factory=dict)
    dispatched_total: int = 0
    cancel_requested: bool = False
    cancel_reason: str | None = None
    paused: bool = False
    seen_signal_ids: list[str] = field(default_factory=list)
    requested_by: str = ""
    approval_decisions: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentRunWorkflowResult:
    """Terminal summary of the run (a projection of PG truth, not the truth)."""

    run_id: str
    status: str
    completed_tasks: list[str]
    failed_tasks: list[str]
    skipped_tasks: list[str]


@workflow.defn(name="agent-run")
class AgentRunWorkflow:
    """Durable orchestration shell for one agent run."""

    def __init__(self) -> None:
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._skipped: set[str] = set()
        self._attempt_counts: dict[str, int] = {}
        self._dispatched_total = 0
        self._cancel_requested = False
        self._cancel_reason: str | None = None
        self._paused = False
        self._seen_signal_ids: set[str] = set()

    @workflow.run
    async def run(self, input: AgentRunWorkflowInput) -> AgentRunWorkflowResult:
        graph = TaskGraph.model_validate(input.graph)
        self._completed = set(input.completed_tasks)
        self._failed = set(input.failed_tasks)
        self._skipped = set(input.skipped_tasks)
        self._attempt_counts = dict(input.attempt_counts)
        self._dispatched_total = input.dispatched_total
        self._cancel_requested = input.cancel_requested
        self._cancel_reason = input.cancel_reason
        self._paused = input.paused
        self._seen_signal_ids = set(input.seen_signal_ids)
        self._approval_decisions: dict[str, str] = dict(input.approval_decisions)

        if not input.started:
            await workflow.execute_activity(
                "start_run",
                StartRunActivityInput(
                    run_id=input.run_id,
                    organization_id=input.organization_id,
                    workspace_id=input.workspace_id,
                    graph=input.graph,
                    actor_ref=input.actor_ref,
                ),
                schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
                retry_policy=_INFRA_RETRY_POLICY,
                task_queue=input.task_queue,
            )

        while True:
            if self._cancel_requested:
                await self._record_terminal(input, "cancelled")
                return self._result(input, "cancelled")

            newly_skipped = self._skip_unreachable(graph, input)
            for task_id in newly_skipped:
                await workflow.execute_activity(
                    "record_task_skipped",
                    RecordTaskSkippedInput(
                        run_id=input.run_id,
                        organization_id=input.organization_id,
                        workspace_id=input.workspace_id,
                        task_id=task_id,
                        reason="dependencies failed",
                        actor_ref=input.actor_ref,
                    ),
                    schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
                    retry_policy=_INFRA_RETRY_POLICY,
                    task_queue=input.task_queue,
                )

            terminal = self._terminal_set()
            ready = [
                task_id
                for task_id in self._ready_sorted(graph)
                if task_id not in terminal
            ]

            if not ready:
                if set(graph.nodes) <= self._terminal_set():
                    outcome = "failed" if self._failed else "completed"
                    await self._record_terminal(input, outcome)
                    return self._result(input, outcome)
                if self._paused:
                    await workflow.wait_condition(
                        lambda: not self._paused or self._cancel_requested
                    )
                    continue
                # 无 ready、非全终态、未暂停：依赖失败传播后剩余节点应已被 skip；
                # 仍卡住说明调度不可推进——fail closed，不静默空转。
                await self._record_terminal(input, "failed")
                return self._result(input, "failed")

            if self._paused:
                await workflow.wait_condition(
                    lambda: not self._paused or self._cancel_requested
                )
                continue

            parallel = [t for t in ready if graph.nodes[t].parallel_safe]
            serial = [t for t in ready if not graph.nodes[t].parallel_safe]

            # RequestApproval 任务走专用等待路径（spec §3/§4：审批期间 run 挂起，
            # 决策经生产命令路径以信号送达；CAR 门与 cancel 语义照常生效）。
            pending_approval = [
                t
                for t in serial
                if graph.nodes[t].task_type == "RequestApproval"
                and t not in self._approval_decisions
            ]
            if pending_approval and not self._cancel_requested:
                task_id = pending_approval[0]

                def _decided(t: str = task_id) -> bool:
                    return t in self._approval_decisions

                await workflow.execute_activity(
                    "create_approval",
                    CreateApprovalInput(
                        run_id=input.run_id,
                        organization_id=input.organization_id,
                        workspace_id=input.workspace_id,
                        task_id=task_id,
                        requested_by=input.requested_by or "system",
                        actor_ref=input.actor_ref,
                    ),
                    schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
                    retry_policy=_INFRA_RETRY_POLICY,
                    task_queue=input.task_queue,
                )
                # 等待决策信号；cancel 可中断等待
                await workflow.wait_condition(
                    lambda: _decided() or self._cancel_requested
                )
                if self._cancel_requested:
                    continue
                decision = self._approval_decisions[task_id]
                await self._record_approval_outcome(input, graph, task_id, decision)
                continue

            # 串行节点逐个执行；并行只读节点并发分派、按 stable task id 顺序收集。
            for task_id in serial:
                await self._run_task(input, graph, task_id)
                if self._cancel_requested:
                    break

            if parallel and not self._cancel_requested:
                handles = {
                    task_id: self._start_task(input, graph, task_id)
                    for task_id in parallel
                }
                for task_id in sorted(handles):
                    await self._await_and_interpret(input, graph, task_id, handles[task_id])

            self._dispatched_total += len(serial) + len(parallel)
            # cancel/pause 意图优先于 CAN：有未决信号时直接留在本 run 处理，
            # 不把信号语义交给 CAN 边界（Temporal 在 CAN 间隙到达的信号会被
            # server 丢弃且 RPC 已成功）。
            if (
                input.continue_as_new_after > 0
                and not self._cancel_requested
                and not self._paused
                and self._dispatched_total >= input.continue_as_new_after
                and not set(graph.nodes) <= self._terminal_set()
            ):
                # continue_as_new 声明为 NoReturn（旧 history 就此截止，新 run 承接
                # 状态）；await NoReturn 是 SDK 类型存根的已知形态，运行时永不返回。
                await workflow.continue_as_new(  # pyright: ignore[reportGeneralTypeIssues]
                    self._carry_input(input)
                )
                raise AssertionError("continue_as_new must not return")

    # ------------------------------------------------------------------ signals

    @workflow.signal
    def cancel(self, payload: dict[str, Any]) -> None:
        if self._dedupe_signal(payload):
            return
        self._cancel_requested = True
        self._cancel_reason = payload.get("reason")

    @workflow.signal
    def pause(self, payload: dict[str, Any]) -> None:
        if self._dedupe_signal(payload):
            return
        self._paused = True

    @workflow.signal
    def resume(self, payload: dict[str, Any]) -> None:
        if self._dedupe_signal(payload):
            return
        self._paused = False

    @workflow.signal
    def approval_decided(self, payload: dict[str, Any]) -> None:
        """审批人经生产命令路径投递的决策（task_id + approved/rejected）。"""
        if self._dedupe_signal(payload):
            return
        task_id = str(payload.get("task_id", ""))
        decision = str(payload.get("decision", ""))
        if task_id and decision in {"approved", "rejected"}:
            self._approval_decisions[task_id] = decision

    @workflow.signal
    def notify(self, payload: dict[str, Any]) -> None:
        """Generic signal channel (S3+ handler hints); deduplicated by command id."""

        self._dedupe_signal(payload)

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "completed": sorted(self._completed),
            "failed": sorted(self._failed),
            "skipped": sorted(self._skipped),
            "cancelled": self._cancel_requested,
            "paused": self._paused,
            "dispatched_total": self._dispatched_total,
        }

    # ------------------------------------------------------------------ helpers

    def _dedupe_signal(self, payload: dict[str, Any]) -> bool:
        """Return True when the signal is a duplicate (already seen command id)."""

        signal_id = str(payload.get("command_event_id", ""))
        if not signal_id or signal_id in self._seen_signal_ids:
            return bool(signal_id)
        self._seen_signal_ids.add(signal_id)
        return False

    def _terminal_set(self) -> set[str]:
        return self._completed | self._failed | self._skipped

    def _ready_sorted(self, graph: TaskGraph) -> list[str]:
        ready = graph.ready_tasks(set(self._completed))
        return sorted(ready)

    def _result(
        self, input: AgentRunWorkflowInput, status: str
    ) -> AgentRunWorkflowResult:
        return AgentRunWorkflowResult(
            run_id=input.run_id,
            status=status,
            completed_tasks=sorted(self._completed),
            failed_tasks=sorted(self._failed),
            skipped_tasks=sorted(self._skipped),
        )

    def _carry_input(self, input: AgentRunWorkflowInput) -> AgentRunWorkflowInput:
        return AgentRunWorkflowInput(
            run_id=input.run_id,
            organization_id=input.organization_id,
            workspace_id=input.workspace_id,
            graph=input.graph,
            task_queue=input.task_queue,
            max_task_attempts=input.max_task_attempts,
            continue_as_new_after=input.continue_as_new_after,
            activity_timeout_seconds=input.activity_timeout_seconds,
            actor_ref=input.actor_ref,
            started=True,
            completed_tasks=sorted(self._completed),
            failed_tasks=sorted(self._failed),
            skipped_tasks=sorted(self._skipped),
            attempt_counts=dict(self._attempt_counts),
            dispatched_total=self._dispatched_total,
            cancel_requested=self._cancel_requested,
            cancel_reason=self._cancel_reason,
            paused=self._paused,
            seen_signal_ids=sorted(self._seen_signal_ids),
            requested_by=input.requested_by,
            approval_decisions=dict(self._approval_decisions),
        )

    def _skip_unreachable(
        self, graph: TaskGraph, input: AgentRunWorkflowInput
    ) -> list[str]:
        """Mark tasks whose dependencies terminally failed/skipped as skipped.

        依赖失败传播是编排决策（不是数据写入），在 workflow 内确定性计算；TaskSkipped
        事件由 activity 落账（幂等键 run:task:skipped，重复传播是 no-op）。返回本轮
        新增 skip 的 task id 列表（sorted，确定性）。
        """
        newly_skipped: list[str] = []
        changed = True
        while changed:
            changed = False
            for task_id in sorted(graph.nodes):
                if task_id in self._terminal_set():
                    continue
                blocked = [
                    dep
                    for dep in graph.nodes[task_id].dependencies
                    if dep in self._failed or dep in self._skipped
                ]
                if blocked:
                    self._skipped.add(task_id)
                    newly_skipped.append(task_id)
                    changed = True
        return newly_skipped

    def _start_task(
        self, input: AgentRunWorkflowInput, graph: TaskGraph, task_id: str
    ) -> Any:
        attempt_no = self._attempt_counts.get(task_id, 0) + 1
        self._attempt_counts[task_id] = attempt_no
        node = graph.nodes[task_id]
        activity_input = ExecuteTaskInput(
            run_id=input.run_id,
            organization_id=input.organization_id,
            workspace_id=input.workspace_id,
            task_id=task_id,
            task_type=node.task_type,
            handler_version=1,
            attempt_id=str(workflow.uuid4()),
            attempt_no=attempt_no,
            input_values={},
            actor_ref=input.actor_ref,
        )
        return workflow.start_activity(
            "execute_task",
            activity_input,
            schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
            retry_policy=_INFRA_RETRY_POLICY,
            task_queue=input.task_queue,
        )

    async def _run_task(
        self, input: AgentRunWorkflowInput, graph: TaskGraph, task_id: str
    ) -> None:
        await self._await_and_interpret(
            input, graph, task_id, self._start_task(input, graph, task_id)
        )

    async def _await_and_interpret(
        self,
        input: AgentRunWorkflowInput,
        graph: TaskGraph,
        task_id: str,
        handle: Any,
    ) -> None:
        from temporalio.exceptions import ActivityError

        try:
            result = await handle
        except ActivityError as exc:
            await self._interpret(input, graph, task_id, exc)
            return
        await self._interpret(input, graph, task_id, result)

    async def _interpret(
        self,
        input: AgentRunWorkflowInput,
        graph: TaskGraph,
        task_id: str,
        result: Any,
    ) -> None:
        from temporalio.exceptions import ActivityError

        if isinstance(result, ActivityError):
            # activity 基础设施重试耗尽（handler 挂起超时等）→ TaskFailed 终态
            await workflow.execute_activity(
                "record_task_failed",
                RecordTaskFailedInput(
                    run_id=input.run_id,
                    organization_id=input.organization_id,
                    workspace_id=input.workspace_id,
                    task_id=task_id,
                    attempt_no=self._attempt_counts.get(task_id, 1),
                    error=f"activity exhausted: {result}",
                    actor_ref=input.actor_ref,
                ),
                schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
                retry_policy=_INFRA_RETRY_POLICY,
                task_queue=input.task_queue,
            )
            self._failed.add(task_id)
            return

        # string-name 调用经默认 converter 解码为 dict（无 activity 侧类型信息）
        if result["status"] == "completed":
            self._completed.add(task_id)
            return

        # 业务失败：按节点 failure policy 决定重试或终态
        node = graph.nodes[task_id]
        attempt_no = self._attempt_counts.get(task_id, 1)
        can_retry = (
            node.failure_policy == FailurePolicy.RETRY
            and attempt_no < input.max_task_attempts
        )
        if can_retry:
            await self._run_task(input, graph, task_id)
            return
        if node.failure_policy == FailurePolicy.CANCEL:
            self._cancel_requested = True
            self._cancel_reason = f"task {task_id} failed with cancel policy"
        self._failed.add(task_id)

    async def _record_approval_outcome(
        self,
        input: AgentRunWorkflowInput,
        graph: TaskGraph,
        task_id: str,
        decision: str,
    ) -> None:
        """把审批决策落账为任务终态（approved→completed；rejected→failed）。"""
        attempt_no = self._attempt_counts.get(task_id, 0) + 1
        self._attempt_counts[task_id] = attempt_no
        await workflow.execute_activity(
            "record_approval_outcome",
            RecordApprovalOutcomeInput(
                run_id=input.run_id,
                organization_id=input.organization_id,
                workspace_id=input.workspace_id,
                task_id=task_id,
                attempt_no=attempt_no,
                decision=decision,
                actor_ref=input.actor_ref,
            ),
            schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
            retry_policy=_INFRA_RETRY_POLICY,
            task_queue=input.task_queue,
        )
        if decision == "approved":
            self._completed.add(task_id)
        else:
            node = graph.nodes[task_id]
            if node.failure_policy == FailurePolicy.CANCEL:
                self._cancel_requested = True
                self._cancel_reason = f"task {task_id} rejected by approver"
            self._failed.add(task_id)

    async def _record_terminal(
        self, input: AgentRunWorkflowInput, outcome: str
    ) -> None:
        await workflow.execute_activity(
            "record_run_terminal",
            RecordRunTerminalInput(
                run_id=input.run_id,
                organization_id=input.organization_id,
                workspace_id=input.workspace_id,
                outcome=outcome,
                error=self._cancel_reason,
                reason=self._cancel_reason,
                actor_ref=input.actor_ref,
            ),
            schedule_to_close_timeout=timedelta(seconds=input.activity_timeout_seconds),
            retry_policy=_INFRA_RETRY_POLICY,
            task_queue=input.task_queue,
        )
