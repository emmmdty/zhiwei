"""S2-T6：AgentRuntimeExecutor——eval 单位经生产 Runtime 命令路径执行。

事实源：specs/s2-agent-runtime.md §6（Eval：S0 Eval executor port 绑定同一 Agent
Runtime）、AGENTS.md「评测走生产 Runtime，不写评测专用旁路」。

每个 unit 的执行链路与生产完全同构：RunCommandService（Run 行 + outbox 命令，同事务）
→ OutboxDispatcher → TemporalWorkflowSender（真实 dev server）→ AgentRunWorkflow →
RuntimeActivities（PG canonical events）。invariants 只读 reduced RunState——没有
eval 专用 Planner/Workflow/reducer 路径。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.runtime_contracts import (
    RuntimeContractScenario,
    check_invariant,
    scenario_for_unit,
)
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
from zhiwei.runtime.reducer import RunState
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker
from zhiwei.workers.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherConfig,
    SessionOutboxRepository,
)
from zhiwei.workers.temporal_sender import TemporalWorkflowSender

logger = logging.getLogger(__name__)

_TERMINAL_TIMEOUT = timedelta(seconds=60)
_POLL_INTERVAL = 0.1


class _StableHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "Fixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"task_id": input.task_id})


class _ObserveHandler(TaskHandler):
    """输出 append 型 observation（parallel merge 契约）。"""

    @property
    def primitive_type(self) -> str:
        return "ObserveFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(
            output_values={"observations": [f"observation:{input.task_id}"]}
        )


class _DecisionHandler(TaskHandler):
    """输出 conflict_preserving 型 decision（ADR-005 契约）。"""

    @property
    def primitive_type(self) -> str:
        return "DecisionFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"decision": f"decision:{input.task_id}"})


class _FlakyHandler(TaskHandler):
    """第 1 次调用失败，之后成功（业务失败 → workflow 重试契约）。"""

    def __init__(self) -> None:
        self._calls = 0

    @property
    def primitive_type(self) -> str:
        return "FlakyFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("injected transient failure")
        return TaskOutput(output_values={"task_id": input.task_id})


class _AlwaysFailsHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "AlwaysFails"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        raise RuntimeError("injected permanent failure")


class _SlowHandler(TaskHandler):
    def __init__(self, *, sleep_seconds: float = 0.5) -> None:
        self._sleep_seconds = sleep_seconds

    @property
    def primitive_type(self) -> str:
        return "SlowFixture"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        import time

        time.sleep(self._sleep_seconds)
        return TaskOutput(output_values={"task_id": input.task_id})


# handler kind -> factory（executor 侧的注册表；task_type 由场景图节点声明）
_HANDLER_FACTORIES: dict[str, type[TaskHandler] | Any] = {
    "stable": _StableHandler,
    "observe": _ObserveHandler,
    "decision": _DecisionHandler,
    "flaky": _FlakyHandler,
    "always-fails": _AlwaysFailsHandler,
    "slow": _SlowHandler,
}


def build_contract_registry() -> TaskHandlerRegistry:
    """注册全部契约 handler（primitive_type 唯一，重复注册拒绝）。"""
    registry = TaskHandlerRegistry()
    for factory in _HANDLER_FACTORIES.values():
        registry.register(factory())
    return registry


class RuntimeEvalEnvironment:
    """真实 runtime eval 环境：PG 会话 + Temporal dev server + worker + dispatcher。"""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        context: TenantContext,
        client: Client,
        owns_environment: WorkflowEnvironment | None = None,
    ) -> None:
        self._sessions = sessions
        self._context = context
        self._client = client
        self._owns_environment = owns_environment
        self._worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> RuntimeEvalEnvironment:
        self._worker = build_agent_worker(
            self._client,
            task_queue=DEFAULT_TASK_QUEUE,
            session_factory=self._sessions,
            handler_registry=build_contract_registry(),
        )
        self._worker_task = asyncio.create_task(self._worker.run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._worker is not None:
            await self._worker.shutdown()
        if self._worker_task is not None:
            await self._worker_task
        if self._owns_environment is not None:
            await self._owns_environment.shutdown()

    @classmethod
    async def start(
        cls,
        *,
        sessions: async_sessionmaker[AsyncSession],
        context: TenantContext,
    ) -> RuntimeEvalEnvironment:
        """启动进程内 Temporal dev server（local-product 同款）并绑定 worker。"""
        import os

        # dev server 启动横幅会写继承的 stdout；CLI 契约要求 stdout 纯 JSON，
        # 在启动窗口内重定向 fd 1（横幅是同步打印的，窗口结束即恢复）。
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_fd = os.dup(1)
        os.dup2(devnull_fd, 1)
        try:
            env = await WorkflowEnvironment.start_local()
            await asyncio.sleep(0.1)  # 横幅与端口打印均在启动窗口内完成
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            os.close(devnull_fd)
        return cls(
            sessions=sessions, context=context, client=env.client, owns_environment=env
        )

    @property
    def client(self) -> Client:
        return self._client

    def dispatcher(self) -> OutboxDispatcher:
        repository = SessionOutboxRepository(self._sessions, self._context)
        sender = TemporalWorkflowSender(self._client)
        handler = OutboxSignalHandler(sender)
        return OutboxDispatcher(
            repository,
            handler,
            OutboxDispatcherConfig(
                worker_id="runtime-eval-dispatcher",
                poll_interval=timedelta(milliseconds=50),
                batch_limit=20,
                max_attempts=5,
                base_delay=timedelta(milliseconds=50),
                lease_duration=timedelta(seconds=30),
            ),
        )


class AgentRuntimeExecutor:
    """经生产命令路径执行 runtime-contract 单位并断言 invariants。"""

    def __init__(self, environment: RuntimeEvalEnvironment) -> None:
        self._environment = environment
        self._sessions = environment._sessions
        self._context = environment._context

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        scenario = scenario_for_unit(unit)
        try:
            return await self._execute_scenario(scenario)
        except Exception as exc:
            logger.exception("runtime contract unit %s errored", unit.unit_id)
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.ERROR,
                result={"error": str(exc), "unit_id": unit.unit_id},
            )

    async def _execute_scenario(self, scenario: RuntimeContractScenario) -> SampleOutcome:
        dispatcher = self._environment.dispatcher()
        run_id = uuid4()

        # 1. 生产命令路径：Run 行 + start 命令（同一事务）
        async with tenant_session(self._sessions, self._context) as session:
            service = RunCommandService(session, self._context)
            await service.submit_start_run(
                run_id=run_id,
                graph=scenario.graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                max_task_attempts=scenario.max_task_attempts,
                continue_as_new_after=scenario.continue_as_new_after,
            )

        # 2. dispatch start（等待投递完成）
        await self._drain_commands(dispatcher)

        # 3. 信号脚本（重复投递经 reclaim 模拟 at-least-once）
        for script in scenario.signals:
            await self._wait_workflow_running(run_id)
            async with tenant_session(self._sessions, self._context) as session:
                service = RunCommandService(session, self._context)
                if script.kind == "cancel_run":
                    await service.submit_cancel_run(run_id=run_id, reason=script.reason)
                elif script.kind == "pause_run":
                    await service.submit_pause_run(run_id=run_id, reason=script.reason)
                elif script.kind == "resume_run":
                    await service.submit_resume_run(run_id=run_id)
                else:
                    raise ValueError(f"unsupported signal kind: {script.kind}")
            await self._drain_commands(dispatcher)
            if script.duplicate:
                # at-least-once：同一命令的重复投递（workflow 侧 command_event_id 去重）
                await self._redeliver_last_command(dispatcher)

        # 4. 等待终态（真相在 PG）
        state = await self._wait_terminal(run_id)

        # 5. invariants（附 CAN 可观测证据：同 workflow id 的 execution 数）
        async with tenant_session(self._sessions, self._context) as session:
            store = RuntimeEventStore(session, self._context)
            events = await store.load_events(run_id)
        executions = [
            e
            async for e in self._environment.client.list_workflows(
                f"WorkflowId = 'run-{run_id}'"
            )
        ]
        state = state.model_copy(update={"_execution_count": len(executions)})
        errors = check_invariant(scenario.invariant, state, events)

        result = {
            "unit_id": scenario.unit.unit_id,
            "run_id": str(run_id),
            "run_status": state.status,
            "tasks": {tid: t.status for tid, t in state.tasks.items()},
            "event_count": len(events),
            "conflict_count": len(state.conflicts),
        }
        if errors:
            return SampleOutcome(
                unit=scenario.unit,
                status=SampleStatus.FAILED,
                result={**result, "invariant_violations": errors},
            )
        return SampleOutcome(
            unit=scenario.unit,
            status=SampleStatus.COMPLETED,
            result={**result, "invariant": scenario.invariant},
        )

    async def _drain_commands(self, dispatcher: OutboxDispatcher) -> None:
        """轮询直至没有 pending/processing 的 runtime 命令（全部 delivered 或失败留待重试）。"""
        for _ in range(200):
            results = await dispatcher.poll_once()
            pending = await self._pending_command_count()
            if not results and pending == 0:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("runtime commands did not drain")

    async def _pending_command_count(self) -> int:
        from sqlalchemy import select

        from zhiwei.persistence.models import OutboxMessage
        from zhiwei.persistence.run_commands import RUNTIME_COMMAND_TOPIC

        async with tenant_session(self._sessions, self._context) as session:
            rows = (
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.organization_id == self._context.organization_id,
                        OutboxMessage.workspace_id == self._context.workspace_id,
                        OutboxMessage.topic == RUNTIME_COMMAND_TOPIC,
                        OutboxMessage.status.in_(("pending", "processing")),
                    )
                )
            ).all()
            return len(rows)

    async def _redeliver_last_command(self, dispatcher: OutboxDispatcher) -> None:
        """取最近一条已投递命令，以 1ms 租约重放投递（at-least-once 语义）。"""
        from sqlalchemy import select

        from zhiwei.persistence.models import OutboxMessage
        from zhiwei.persistence.run_commands import RUNTIME_COMMAND_TOPIC

        async with tenant_session(self._sessions, self._context) as session:
            row = await session.scalar(
                select(OutboxMessage)
                .where(
                    OutboxMessage.organization_id == self._context.organization_id,
                    OutboxMessage.workspace_id == self._context.workspace_id,
                    OutboxMessage.topic == RUNTIME_COMMAND_TOPIC,
                    OutboxMessage.status == "delivered",
                )
                .order_by(OutboxMessage.created_at.desc())
                .limit(1)
            )
            if row is None:
                raise RuntimeError("no delivered command to redeliver")
            # 模拟「投递后崩溃」：回退为 pending（真相仍是 at-least-once 重投）
            row.status = "pending"
            row.attempts = 0
        await asyncio.sleep(0.01)
        await self._drain_commands(dispatcher)

    async def _wait_workflow_running(self, run_id: UUID) -> None:
        handle = self._environment.client.get_workflow_handle(f"run-{run_id}")
        for _ in range(200):
            try:
                await handle.describe()
                return
            except Exception:
                await asyncio.sleep(0.05)
        raise TimeoutError(f"workflow run-{run_id} never started")

    async def _wait_terminal(self, run_id: UUID) -> RunState:
        deadline = datetime.now(tz=UTC) + _TERMINAL_TIMEOUT
        while datetime.now(tz=UTC) < deadline:
            async with tenant_session(self._sessions, self._context) as session:
                store = RuntimeEventStore(session, self._context)
                state = await store.reduce_state(run_id)
                if state.is_terminal:
                    return state
            await asyncio.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"run {run_id} did not reach terminal state")


async def execute_runtime_contract_suite(
    *,
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
) -> list[SampleOutcome]:
    """执行全部 runtime-contract 单位（CLI 与 replay-check 共用入口）。"""
    from zhiwei.evals.runtime_contracts import RUNTIME_CONTRACT_UNITS

    runtime_env = await RuntimeEvalEnvironment.start(sessions=sessions, context=context)
    async with runtime_env as env:
        executor = AgentRuntimeExecutor(env)
        outcomes: list[SampleOutcome] = []
        for unit in RUNTIME_CONTRACT_UNITS:
            outcomes.append(await executor.execute(unit))
        return outcomes
