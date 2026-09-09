"""S6 ask-v1 executor：行为契约场景经生产 Runtime 命令路径执行。

事实源：specs/s6-evidence-ask.md §6、AGENTS.md「评测走生产 Runtime，不写评测专用旁路」。

执行链路与生产完全同构（与 S2 AgentRuntimeExecutor 同款）：RunCommandService
（Run 行 + outbox 命令，同事务）→ OutboxDispatcher → Temporal dev server →
AgentRunWorkflow → RuntimeActivities（PG canonical events）。场景定义见
evals/ask_contracts.py；invariants 只读 reduced RunState——没有 eval 专用
Planner/Workflow/reducer 路径。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.evals.ask_contracts import (
    ASK_V1_UNITS,
    AskScenario,
    build_ask_contract_registry,
    check_invariant,
    scenario_for_unit,
)
from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.executors.agent_runtime import (
    DEFAULT_TASK_QUEUE,
    RuntimeEvalEnvironment,
)
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.reducer import RunState

logger = logging.getLogger(__name__)

_TERMINAL_TIMEOUT = timedelta(seconds=60)
_POLL_INTERVAL = 0.1


async def build_ask_environment(
    *,
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
) -> RuntimeEvalEnvironment:
    """启动 ask-v1 的真实 runtime eval 环境（ask handler 注册表 + dev server + worker）。

    返回的实例已进入运行态（worker 已启动）；用 ``aclose()`` 显式关闭。
    """
    environment = await RuntimeEvalEnvironment.start(
        sessions=sessions,
        context=context,
        handler_registry=build_ask_contract_registry(),
    )
    await environment.__aenter__()
    return environment


class AskRuntimeExecutor:
    """经生产命令路径执行 ask-v1 行为场景并断言 invariants。"""

    def __init__(self, environment: RuntimeEvalEnvironment) -> None:
        self._environment = environment
        self._sessions = environment.sessions
        self._context = environment.tenant_context

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessions

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        scenario = scenario_for_unit(unit)
        try:
            return await self._execute_scenario(scenario)
        except Exception as exc:
            logger.exception("ask contract unit %s errored", unit.unit_id)
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.ERROR,
                result={"error": str(exc), "unit_id": unit.unit_id},
            )

    async def _execute_scenario(self, scenario: AskScenario) -> SampleOutcome:
        from zhiwei.runtime.outbox_handlers import OutboxSignalHandler
        from zhiwei.workers.outbox_dispatcher import (
            OutboxDispatcher,
            OutboxDispatcherConfig,
            SessionOutboxRepository,
        )
        from zhiwei.workers.temporal_sender import TemporalWorkflowSender

        dispatcher = OutboxDispatcher(
            SessionOutboxRepository(self._sessions, self._context),
            OutboxSignalHandler(TemporalWorkflowSender(self._environment.client)),
            OutboxDispatcherConfig(
                worker_id="ask-eval-dispatcher",
                poll_interval=timedelta(milliseconds=50),
                batch_limit=20,
                max_attempts=5,
                base_delay=timedelta(milliseconds=50),
                lease_duration=timedelta(seconds=30),
            ),
        )
        run_id = uuid4()

        # 1. 生产命令路径：Run 行 + start 命令（同一事务）
        async with tenant_session(self._sessions, self._context) as session:
            service = RunCommandService(session, self._context)
            await service.submit_start_run(
                run_id=run_id,
                graph=scenario.graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
            )

        # 2. dispatch start 并等待终态（真相在 PG）
        await self._drain_commands(dispatcher)
        state = await self._wait_terminal(run_id)

        # 3. invariants（只读 reduced RunState 与事件序列）
        async with tenant_session(self._sessions, self._context) as session:
            store = RuntimeEventStore(session, self._context)
            events = await store.load_events(run_id)
        errors = check_invariant(scenario.invariant, state, events)

        result = {
            "unit_id": scenario.unit.unit_id,
            "run_id": str(run_id),
            "run_status": state.status,
            "tasks": {tid: t.status for tid, t in state.tasks.items()},
            "event_count": len(events),
            "conflict_count": len(state.conflicts),
            "canonical_keys": sorted(state.canonical),
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

    async def _drain_commands(self, dispatcher: Any) -> None:
        """轮询直至没有 pending/processing 的 runtime 命令。"""
        for _ in range(200):
            results = await dispatcher.poll_once()
            pending = await self._pending_command_count()
            if not results and pending == 0:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("ask runtime commands did not drain")

    async def _pending_command_count(self) -> int:
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

    async def _wait_terminal(self, run_id: Any) -> RunState:
        deadline = datetime.now(tz=UTC) + _TERMINAL_TIMEOUT
        while datetime.now(tz=UTC) < deadline:
            async with tenant_session(self._sessions, self._context) as session:
                store = RuntimeEventStore(session, self._context)
                state = await store.reduce_state(run_id)
                if state.is_terminal:
                    return state
            await asyncio.sleep(_POLL_INTERVAL)
        raise TimeoutError(f"ask run {run_id} did not reach terminal state")


async def execute_ask_contract_suite(
    *,
    sessions: async_sessionmaker[AsyncSession],
    context: TenantContext,
) -> list[SampleOutcome]:
    """执行全部 ask-v1 行为场景（CLI 与 integration 共用入口）。"""
    environment = await build_ask_environment(sessions=sessions, context=context)
    try:
        executor = AskRuntimeExecutor(environment)
        outcomes: list[SampleOutcome] = []
        for unit in ASK_V1_UNITS:
            outcomes.append(await executor.execute(unit))
        return outcomes
    finally:
        await environment.aclose()
