"""S2-T3 集成：真实 Temporal durable shell 的 run 生命周期契约。

事实源：specs/s2-agent-runtime.md §6（Temporal：deterministic start、activity
timeout/retry、worker kill/restart、signal duplicate、replay、Continue-As-New）。

所有断言优先落在 PG canonical events（业务真相），workflow result 只是投影。
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from uuid import uuid4

import pytest

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.contracts.identifiers import new_id
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.workers.agent_worker import DEFAULT_TASK_QUEUE, build_agent_worker
from zhiwei.workflows.agent_run import AgentRunWorkflow, AgentRunWorkflowInput

pytestmark = pytest.mark.asyncio


def _graph(
    *,
    task_types: dict[str, str] | None = None,
    deps: dict[str, list[str]] | None = None,
    parallel: set[str] | None = None,
) -> TaskGraph:
    task_types = task_types or {}
    deps = deps or {}
    parallel = parallel or set()
    nodes = {}
    for task_id, task_type in task_types.items():
        nodes[task_id] = TaskGraphNode(
            task_id=task_id,
            task_type=task_type,
            dependencies=tuple(deps.get(task_id, ())),
            parallel_safe=task_id in parallel,
            required_capability="fixture",
        )
    return TaskGraph(nodes=nodes, edges={k: list(v) for k, v in deps.items()})


async def _submit_run(
    sessions, context: TenantContext, graph: TaskGraph, **input_kwargs
) -> str:
    from zhiwei.persistence.run_commands import RunCommandService

    run_id = uuid4()
    async with tenant_session(sessions, context) as session:
        service = RunCommandService(session, context)
        await service.submit_start_run(
            run_id=run_id,
            graph=graph.model_dump(mode="json"),
            task_queue=DEFAULT_TASK_QUEUE,
        )
    return str(run_id)


class TestDeterministicStart:
    async def test_run_completes_and_events_land_in_pg(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, _registry = worker_stack
        graph = _graph(
            task_types={"intake": "Fixture", "analyze": "Fixture"},
            deps={"analyze": ["intake"]},
        )
        run_id = await _submit_run(sessions, context, graph)

        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result.status == "completed"
        assert sorted(result.completed_tasks) == ["analyze", "intake"]

        # 真相在 PG：事件链完整、可 reduce 到终态
        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "completed"
            assert set(state.tasks) == {"intake", "analyze"}
            assert all(t.status == "completed" for t in state.tasks.values())
            events = await store.load_events(uuid_module.UUID(run_id))
            types = [type(e).__name__ for e in events]
            assert "RunCreated" in types and "RunCompleted" in types

    async def test_deterministic_workflow_id_rejects_second_start(
        self, temporal_env, worker_stack
    ) -> None:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        _, sessions, context, _registry = worker_stack
        graph = _graph(task_types={"t1": "Fixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        input = AgentRunWorkflowInput(
            run_id=run_id,
            organization_id=str(context.organization_id),
            workspace_id=str(context.workspace_id),
            graph=graph.model_dump(mode="json"),
            task_queue=DEFAULT_TASK_QUEUE,
            activity_timeout_seconds=5,
        )
        from temporalio.common import WorkflowIDReusePolicy

        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            input,
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        await asyncio.wait_for(handle.result(), timeout=30)
        # run 已结束，同 id 再次 start 必须被拒（REJECT_DUPLICATE 语义）
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                AgentRunWorkflow.run,
                input,
                id=f"run-{run_id}",
                task_queue=DEFAULT_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )


class TestActivityRetry:
    async def test_transient_handler_failure_retries_within_task_attempts(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, registry = worker_stack
        # 业务失败重试：第 1 个 attempt 失败，第 2 个成功 → task completed, attempts=2
        from tests.integration.temporal.conftest import FlakyFixtureHandler

        registry.register(FlakyFixtureHandler(fail_times=1))
        graph = _graph(task_types={"t1": "FlakyFixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result.status == "completed"

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.tasks["t1"].status == "completed"
            # attempt 1 aborted、attempt 2 committed —— 重试留痕
            statuses = {a.id: a.status for a in state.tasks["t1"].attempts.values()}
            assert sorted(statuses.values()) == ["aborted", "committed"]


class TestSignalDuplicate:
    async def test_duplicate_cancel_signal_writes_single_event(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, _registry = worker_stack
        graph = _graph(task_types={"t1": "Fixture", "t2": "Fixture", "t3": "Fixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # 同一 command_event_id 的 cancel 信号发两次 —— workflow 去重
        command_event_id = str(new_id())
        await handle.signal("cancel", {"command_event_id": command_event_id, "reason": "dup"})
        await handle.signal("cancel", {"command_event_id": command_event_id, "reason": "dup"})
        result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result.status == "cancelled"

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            events = await store.load_events(uuid_module.UUID(run_id))
            cancels = [e for e in events if type(e).__name__ == "RunCancelled"]
            assert len(cancels) == 1
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "cancelled"


class TestContinueAsNew:
    async def test_continue_as_new_preserves_progress(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, _registry = worker_stack
        graph = _graph(task_types={"t1": "Fixture", "t2": "Fixture", "t3": "Fixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                continue_as_new_after=2,  # 3 个 task → 派发 2 个后 Continue-As-New
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result.status == "completed"
        assert len(result.completed_tasks) == 3

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "completed"
            assert all(t.status == "completed" for t in state.tasks.values())


class TestWorkerKillRestart:
    async def test_worker_restart_completes_run(
        self, temporal_env, database
    ) -> None:
        """worker 中途被杀 → 重启 → run 照常终态（durable execution 契约）。"""
        from tests.integration.temporal.conftest import SlowFixtureHandler

        _, sessions, context = database
        registry = TaskHandlerRegistry()
        registry.register(SlowFixtureHandler(sleep_seconds=2.0))
        graph = _graph(task_types={"t1": "SlowFixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client

        worker_a = build_agent_worker(
            client,
            task_queue=DEFAULT_TASK_QUEUE,
            session_factory=sessions,
            handler_registry=registry,
        )
        async with worker_a:
            handle = await client.start_workflow(
                AgentRunWorkflow.run,
                AgentRunWorkflowInput(
                    run_id=run_id,
                    organization_id=str(context.organization_id),
                    workspace_id=str(context.workspace_id),
                    graph=graph.model_dump(mode="json"),
                    task_queue=DEFAULT_TASK_QUEUE,
                ),
                id=f"run-{run_id}",
                task_queue=DEFAULT_TASK_QUEUE,
            )
            await asyncio.sleep(0.5)  # 让 worker A 领走 activity，随后杀死
        # worker_a 退出（activity 未完成）；worker B 接手重试。
        # 构造即注册——必须在 a 完全关闭之后构造 b，否则同 queue 注册冲突。
        worker_b = build_agent_worker(
            client,
            task_queue=DEFAULT_TASK_QUEUE,
            session_factory=sessions,
            handler_registry=registry,
        )
        async with worker_b:
            result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "completed"

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "completed"


class TestActivityTimeout:
    async def test_hanging_activity_times_out_and_fails_run(
        self, temporal_env, worker_stack
    ) -> None:
        """schedule_to_close 超时 → 重试耗尽 → TaskFailed → run failed。"""
        from tests.integration.temporal.conftest import SlowFixtureHandler

        _, sessions, context, registry = worker_stack
        registry.register(SlowFixtureHandler(sleep_seconds=30.0))
        graph = _graph(task_types={"t1": "SlowFixture"})
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                activity_timeout_seconds=1,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        result = await asyncio.wait_for(handle.result(), timeout=90)
        assert result.status == "failed"
        assert result.failed_tasks == ["t1"]

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "failed"
            assert state.tasks["t1"].status == "failed"


class TestWorkflowReplay:
    async def test_completed_history_replays_deterministically(
        self, temporal_env, worker_stack
    ) -> None:
        """Replayer：完成的 workflow history 重放必须无 nondeterminism 错误。"""
        from temporalio.worker import Replayer

        _, sessions, context, _registry = worker_stack
        graph = _graph(
            task_types={"a": "Fixture", "b": "Fixture"},
            deps={"b": ["a"]},
        )
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        await asyncio.wait_for(handle.result(), timeout=30)

        history = await handle.fetch_history()
        replayer = Replayer(workflows=[AgentRunWorkflow])
        await replayer.replay_workflow(history=history)


class TestSignalsAcrossContinueAsNew:
    """S2 修复轮 RED（Reviewer B #1）：信号状态必须跨 Continue-As-New 存活。

    CAN 只携带任务集合时，cancel/pause/seen_signal_ids 全部丢失——CANCEL failure
    policy 或 operator cancel 落在 CAN 之后即被静默吞掉（run 照常 completed）。
    """

    async def test_cancel_after_continue_as_new_still_cancels(
        self, temporal_env, worker_stack
    ) -> None:
        from tests.integration.temporal.conftest import SlowFixtureHandler

        _, sessions, context, registry = worker_stack
        # 2s/task：保证 cancel 稳定落在「之后还会 CAN」的 run 内（非终 run）
        registry.register(SlowFixtureHandler(sleep_seconds=2.0))
        graph = _graph(
            task_types={"t1": "SlowFixture", "t2": "SlowFixture",
                        "t3": "SlowFixture", "t4": "SlowFixture"},
            deps={"t2": ["t1"], "t3": ["t2"], "t4": ["t3"]},
        )
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                continue_as_new_after=1,  # 每 run 只派发 1 个 task → 必然多次 CAN
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # 等 CAN 至少发生一次：同 workflow id 的 execution 数 > 1
        # （describe() 不跟随 CAN 链，run_id 恒为首 run，不能用 run_id 判定）
        deadline = asyncio.get_event_loop().time() + 20
        while asyncio.get_event_loop().time() < deadline:
            executions = [e async for e in client.list_workflows(f"WorkflowId = 'run-{run_id}'")]
            if len(executions) > 1:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("continue-as-new never happened")
        # CAN 之后发送 cancel —— 必须仍然生效
        await handle.signal("cancel", {"command_event_id": str(new_id()), "reason": "post-can"})
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "cancelled", (
            f"cancel after continue-as-new was lost (status={result.status})"
        )

        import uuid as uuid_module

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "cancelled"


class TestApprovalJourney:
    """S2-T7 RED：RequestApproval 任务 → PG 审批请求 → 决策信号 → 任务终态。"""

    def _approval_graph(self) -> TaskGraph:
        return _graph(
            task_types={"intake": "Fixture", "review": "RequestApproval", "final": "Fixture"},
            deps={"review": ["intake"], "final": ["review"]},
        )

    async def _approvals_in_pg(self, sessions, context, run_id):
        from zhiwei.persistence.approvals import ApprovalRequestStore
        from zhiwei.persistence.tenant import tenant_session as ts

        async with ts(sessions, context) as session:
            return await ApprovalRequestStore(session, context).list_for_run(uuid_module.UUID(run_id))

    async def test_rejected_approval_fails_run(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, _registry = worker_stack
        graph = self._approval_graph()
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                requested_by="alice",
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # 等 PG 出现 pending 审批请求（create_approval activity 落账）
        deadline = asyncio.get_event_loop().time() + 30
        approvals: list = []
        while asyncio.get_event_loop().time() < deadline:
            approvals = await self._approvals_in_pg(sessions, context, run_id)
            if approvals:
                break
            await asyncio.sleep(0.1)
        assert approvals, "create_approval activity never recorded a request"
        assert approvals[0].status == "pending"
        assert approvals[0].task_id == "review"

        # 决策经生产命令路径：信号（SoD 由 store 守护——这里直接模拟已裁决信号）
        await handle.signal("approval_decided", {
            "command_event_id": str(new_id()),
            "task_id": "review",
            "decision": "rejected",
        })
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "failed"
        assert "review" in result.failed_tasks

        async with tenant_session(sessions, context) as session:
            state = await RuntimeEventStore(session, context).reduce_state(
                uuid_module.UUID(run_id)
            )
            assert state.status == "failed"

    async def test_approved_approval_completes_run(
        self, temporal_env, worker_stack
    ) -> None:
        _, sessions, context, _registry = worker_stack
        graph = self._approval_graph()
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                requested_by="alice",
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        deadline = asyncio.get_event_loop().time() + 30
        approvals: list = []
        while asyncio.get_event_loop().time() < deadline:
            approvals = await self._approvals_in_pg(sessions, context, run_id)
            if approvals:
                break
            await asyncio.sleep(0.1)
        assert approvals
        await handle.signal("approval_decided", {
            "command_event_id": str(new_id()),
            "task_id": "review",
            "decision": "approved",
        })
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "completed"
        assert "review" in result.completed_tasks


class TestFirstTransactionFailure:
    """S2 修复轮 RED（复审 C-1）：写入侧不得产生 reducer 拒绝的事件序列。

    场景：execute_task 的第一个事务（TaskScheduled/AttemptCreated/TaskStarted 落账）
    遇基础设施故障且重试耗尽——workflow 此时会追加 TaskFailed 终态。若写入侧不补全
    前置生命周期事件，reducer 对 pending→failed 直接抛 ValueError，该 run 的每次
    reduce（详情/SSE/replay-check/eval seal）都崩溃且无自愈路径，违背「PG event 为
    真相」的可消费性（spec §6 写入侧守护，ADR-012 测试层级【I】）。
    """

    async def test_task_failed_without_prior_events_keeps_sequence_reducible(
        self, temporal_env, database
    ) -> None:
        from temporalio import activity
        from temporalio.worker import Worker

        from zhiwei.workflows.activities.runtime import RuntimeActivities

        _, sessions, context = database
        graph = _graph(task_types={"t1": "Fixture"})
        run_id = await _submit_run(sessions, context, graph)

        class _InfraBrokenActivities(RuntimeActivities):
            """execute_task 在第一个事务提交前持续失败（模拟 DB 故障重试耗尽）。

            record_* 与 start_run 保持真实：只有 execute_task 的前置事务被破坏，
            其余落账路径（record_task_failed/record_run_terminal）必须照常工作。
            """

            @activity.defn
            async def execute_task(self, input):  # type: ignore[override]
                raise RuntimeError(
                    "simulated infra failure: first transaction never committed"
                )

        # registry 完整性（批次 C 预检契约）：Fixture handler 必须注册——
        # 预检在 Run 事件落账前拒绝空 registry（sabotage 目标是首事务，
        # 不是 registry）
        from tests.integration.temporal.conftest import CountingFixtureHandler

        registry = TaskHandlerRegistry()
        registry.register(CountingFixtureHandler())
        activities = _InfraBrokenActivities(sessions, registry)
        worker = Worker(
            temporal_env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                activities.start_run,
                activities.execute_task,
                activities.create_approval,
                activities.record_approval_outcome,
                activities.record_run_terminal,
                activities.record_task_skipped,
                activities.record_task_failed,
            ],
        )
        async with worker:
            client = temporal_env.client
            handle = await client.start_workflow(
                AgentRunWorkflow.run,
                AgentRunWorkflowInput(
                    run_id=run_id,
                    organization_id=str(context.organization_id),
                    workspace_id=str(context.workspace_id),
                    graph=graph.model_dump(mode="json"),
                    task_queue=DEFAULT_TASK_QUEUE,
                ),
                id=f"run-{run_id}",
                task_queue=DEFAULT_TASK_QUEUE,
            )
            # 基础设施重试耗尽（5 次退避约 3s）后 ActivityError → TaskFailed 落账
            result = await asyncio.wait_for(handle.result(), timeout=60)
            assert result.status == "failed"
            assert result.failed_tasks == ["t1"]

        # 真相层可消费：reduce 不抛异常，任务投影为 failed、run 到终态
        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            assert state.status == "failed"
            assert state.tasks["t1"].status == "failed"

            # TaskFailed 之前必须存在完整生命周期（写入侧补全，不是只落终态）
            events = await store.load_events(uuid_module.UUID(run_id))
            types = [type(e).__name__ for e in events]
            assert "TaskFailed" in types
            scheduled_at = types.index("TaskScheduled")
            attempt_at = types.index("AttemptCreated")
            started_at = types.index("TaskStarted")
            failed_at = types.index("TaskFailed")
            assert scheduled_at < attempt_at < started_at < failed_at

    async def test_task_failed_records_attempt_aborted(
        self, temporal_env, database
    ) -> None:
        """S2 修复轮批次 B RED（D-3）：首事务失败路径的 attempt 终态对称。

        execute_task 失败路径与 record_approval_outcome 都落 attempt 级终态
        （AttemptAborted/AttemptCommitted）；record_task_failed 只补生命周期
        不落 attempt 终态时，attempt 投影永远停留 pending——eval 的 attempt
        状态断言（aborted/committed 集合精确相等）会漏掉该路径。
        """
        from temporalio import activity
        from temporalio.worker import Worker

        from zhiwei.workflows.activities.runtime import RuntimeActivities

        _, sessions, context = database
        graph = _graph(task_types={"t1": "Fixture"})
        run_id = await _submit_run(sessions, context, graph)

        class _InfraBrokenActivities(RuntimeActivities):
            @activity.defn
            async def execute_task(self, input):  # type: ignore[override]
                raise RuntimeError("simulated infra failure: first transaction never committed")

        from tests.integration.temporal.conftest import CountingFixtureHandler

        _registry = TaskHandlerRegistry()
        _registry.register(CountingFixtureHandler())
        activities = _InfraBrokenActivities(sessions, _registry)
        worker = Worker(
            temporal_env.client,
            task_queue=DEFAULT_TASK_QUEUE,
            workflows=[AgentRunWorkflow],
            activities=[
                activities.start_run,
                activities.execute_task,
                activities.create_approval,
                activities.record_approval_outcome,
                activities.record_run_terminal,
                activities.record_task_skipped,
                activities.record_task_failed,
            ],
        )
        async with worker:
            client = temporal_env.client
            handle = await client.start_workflow(
                AgentRunWorkflow.run,
                AgentRunWorkflowInput(
                    run_id=run_id,
                    organization_id=str(context.organization_id),
                    workspace_id=str(context.workspace_id),
                    graph=graph.model_dump(mode="json"),
                    task_queue=DEFAULT_TASK_QUEUE,
                ),
                id=f"run-{run_id}",
                task_queue=DEFAULT_TASK_QUEUE,
            )
            result = await asyncio.wait_for(handle.result(), timeout=60)
            assert result.status == "failed"

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            task = state.tasks["t1"]
            assert task.attempts, "attempt 投影不得为空"
            attempt_statuses = {a.status for a in task.attempts.values()}
            assert attempt_statuses == {"aborted"}, attempt_statuses


class TestApprovalExpiry:
    """S2 修复轮批次 B RED（H-3）：审批 expiry 必设 + 等待有上界。

    spec §4（2026-09-03 增补）：「审批必须设置 expiry，workflow 等待有上界或由
    过期信号解除」——无 expiry 的 pending 审批使 run 可永久挂起（三条路径：
    崩溃窗口/错 run_id/CAN 间隙丢信号，ADR-012 反例）。
    """

    def _approval_graph(self) -> TaskGraph:
        return _graph(
            task_types={"review": "RequestApproval"},
        )

    async def test_create_approval_sets_expiry(self, temporal_env, worker_stack) -> None:
        """create_approval 落库的请求必须携带 expires_at（None = 永久挂起面）。"""
        _, sessions, context, _registry = worker_stack
        graph = self._approval_graph()
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                requested_by="alice",
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        from zhiwei.persistence.approvals import ApprovalRequestStore

        deadline = asyncio.get_event_loop().time() + 30
        approvals: list = []
        while asyncio.get_event_loop().time() < deadline:
            async with tenant_session(sessions, context) as session:
                approvals = await ApprovalRequestStore(session, context).list_for_run(
                    uuid_module.UUID(run_id)
                )
            if approvals:
                break
            await asyncio.sleep(0.1)
        assert approvals, "create_approval activity never recorded a request"
        assert approvals[0].expires_at is not None, (
            "审批请求必须设置 expiry——None 使 run 可永久挂起（spec §4 增补）"
        )
        # 清理：cancel 让 workflow 结束（本测试只验证 expiry 落库）
        await handle.signal("cancel", {"command_event_id": str(new_id())})
        await asyncio.wait_for(handle.result(), timeout=60)

    async def test_approval_expiry_fails_run(self, temporal_env, worker_stack) -> None:
        """expiry 到期且无决策 → run 以 failed（approval expired）终态，不挂起。"""
        _, sessions, context, _registry = worker_stack
        graph = self._approval_graph()
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                requested_by="alice",
                approval_expiry_seconds=1,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        # 不发决策信号：等待上界必须把 run 推向终态（无上界 = 永久挂起 → 超时红）
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "failed", (
            f"过期审批必须使 run failed，实际 {result.status}"
        )
        async with tenant_session(sessions, context) as session:
            state = await RuntimeEventStore(session, context).reduce_state(
                uuid_module.UUID(run_id)
            )
            assert state.status == "failed"
            assert state.tasks["review"].status == "failed"


class TestBatchCRepairContracts:
    """S2 修复轮批次 C RED：spec §3/§4 增补的剩余接线级契约。

    - ② expired 路径回查权威审批行（决策已落账但信号迟到 → 不误判 expired）
    - ③ effect_unknown 禁自动重试（workflow _interpret 门）
    - ④ RunPaused/RunResumed 落账（PG 真相与暂停状态一致）
    - ⑤ missing handler 在 Run 事件落账前失败（registry 预检）
    - ⑥ 审批 digest 绑定节点内容（非 run/task 身份常量）
    - ⑧ ConflictDetected 作为 canonical event 落账 + ⑨ Synthesize 降级门
    """

    async def _start(self, temporal_env, sessions, context, graph, **kwargs):
        run_id = await _submit_run(sessions, context, graph)
        client = temporal_env.client
        handle = await client.start_workflow(
            AgentRunWorkflow.run,
            AgentRunWorkflowInput(
                run_id=run_id,
                organization_id=str(context.organization_id),
                workspace_id=str(context.workspace_id),
                graph=graph.model_dump(mode="json"),
                task_queue=DEFAULT_TASK_QUEUE,
                **kwargs,
            ),
            id=f"run-{run_id}",
            task_queue=DEFAULT_TASK_QUEUE,
        )
        return run_id, handle

    async def _wait_approval(self, sessions, context, run_id):
        from zhiwei.persistence.approvals import ApprovalRequestStore

        deadline = asyncio.get_event_loop().time() + 30
        approvals: list = []
        while asyncio.get_event_loop().time() < deadline:
            async with tenant_session(sessions, context) as session:
                approvals = await ApprovalRequestStore(session, context).list_for_run(
                    uuid_module.UUID(run_id)
                )
            if approvals:
                return approvals
            await asyncio.sleep(0.1)
        raise AssertionError("pending approval never surfaced")

    async def test_expired_path_uses_authoritative_decision(
        self, temporal_env, worker_stack
    ) -> None:
        """决策已落账但信号迟到：expired 路径必须回查权威行，不误判 expired。"""
        _, sessions, context, _registry = worker_stack
        graph = _graph(task_types={"review": "RequestApproval"})
        run_id, handle = await self._start(
            temporal_env, sessions, context, graph,
            requested_by="alice", approval_expiry_seconds=1,
        )
        approvals = await self._wait_approval(sessions, context, run_id)
        request = approvals[0]

        # 决策经 store 直接落账（模拟信号投递延迟 > timer 的窄窗口）
        from zhiwei.persistence.approvals import ApprovalRequestStore

        async with tenant_session(sessions, context) as session:
            await ApprovalRequestStore(session, context).decide(
                request_id=request.request_id,
                decision="approved",
                approver="someone-else",
                reason="decided before timer",
            )
        # 不发 approval_decided 信号：timer 到期后 workflow 必须以权威行为准
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "completed", (
            f"决策已落账时不得误判 expired（实际 {result.status}）"
        )
        assert "review" in result.completed_tasks

    async def test_effect_unknown_failure_is_not_retried(
        self, temporal_env, worker_stack
    ) -> None:
        """effect_unknown 失败禁自动重试：单次 attempt 即终态（spec §4 增补）。"""
        from tests.integration.temporal.conftest import (
            EffectUnknownFixtureHandler,
        )

        _, sessions, context, registry = worker_stack
        registry.register(EffectUnknownFixtureHandler())
        graph = _graph(task_types={"t1": "EffectUnknownFixture"})
        run_id, handle = await self._start(temporal_env, sessions, context, graph)

        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "failed"

        async with tenant_session(sessions, context) as session:
            events = await RuntimeEventStore(session, context).load_events(
                uuid_module.UUID(run_id)
            )
        attempts = [e for e in events if type(e).__name__ == "AttemptCreated"]
        assert len(attempts) == 1, (
            f"effect_unknown 不得自动重试（实际 {len(attempts)} 次 attempt）"
        )

    async def test_pause_resume_lands_canonical_events(
        self, temporal_env, worker_stack
    ) -> None:
        """pause/resume 信号必须落 RunPaused/RunResumed（PG 真相含暂停态）。"""
        from tests.integration.temporal.conftest import SlowFixtureHandler

        _, sessions, context, registry = worker_stack
        registry.register(SlowFixtureHandler(sleep_seconds=1.0))
        graph = _graph(
            task_types={"t1": "SlowFixture", "t2": "SlowFixture"},
            deps={"t2": ["t1"]},
        )
        run_id, handle = await self._start(temporal_env, sessions, context, graph)

        await asyncio.sleep(0.3)
        await handle.signal("pause", {"command_event_id": str(new_id())})
        # 轮询等待落账：workflow 在当前 activity 完成后才观察暂停标志
        # （单次定点检查在负载下存在时序竞态）
        deadline = asyncio.get_event_loop().time() + 30
        last_status: str | None = None
        paused_seen = False
        while asyncio.get_event_loop().time() < deadline:
            async with tenant_session(sessions, context) as session:
                state = await RuntimeEventStore(session, context).reduce_state(
                    uuid_module.UUID(run_id)
                )
            last_status = state.status
            if last_status == "paused":
                paused_seen = True
                break
            await asyncio.sleep(0.2)
        assert paused_seen, f"暂停期间 PG 真相必须为 paused（实际 {last_status}）"

        await handle.signal("resume", {"command_event_id": str(new_id())})
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "completed"

        async with tenant_session(sessions, context) as session:
            events = await RuntimeEventStore(session, context).load_events(
                uuid_module.UUID(run_id)
            )
        types = [type(e).__name__ for e in events]
        assert "RunPaused" in types and "RunResumed" in types, types

    async def test_missing_handler_fails_before_run_events(
        self, temporal_env, worker_stack
    ) -> None:
        """图含未注册 task type：Run 事件落账前失败（registry 预检，spec §3）。"""
        _, sessions, context, _registry = worker_stack
        graph = _graph(task_types={"t1": "NoSuchHandlerType"})
        run_id, handle = await self._start(temporal_env, sessions, context, graph)

        from temporalio.client import WorkflowFailureError

        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=60)

        async with tenant_session(sessions, context) as session:
            events = await RuntimeEventStore(session, context).load_events(
                uuid_module.UUID(run_id)
            )
        assert events == [], (
            f"missing handler 必须在 Run 事件落账前失败（实际落了 {len(events)} 条）"
        )

    async def test_approval_digest_binds_node_content(
        self, temporal_env, worker_stack
    ) -> None:
        """审批 digest 绑定审批节点的声明内容，非 run/task 身份常量。"""
        _, sessions, context, _registry = worker_stack
        graph_a = _graph(
            task_types={"pre": "Fixture", "review": "RequestApproval"},
            deps={"review": ["pre"]},
        )
        graph_b = _graph(task_types={"review": "RequestApproval"})
        graph_c = _graph(task_types={"review": "RequestApproval"})
        run_a, handle_a = await self._start(
            temporal_env, sessions, context, graph_a, requested_by="alice"
        )
        run_b, handle_b = await self._start(
            temporal_env, sessions, context, graph_b, requested_by="alice"
        )
        run_c, handle_c = await self._start(
            temporal_env, sessions, context, graph_c, requested_by="alice"
        )
        approvals_a = await self._wait_approval(sessions, context, run_a)
        approvals_b = await self._wait_approval(sessions, context, run_b)
        approvals_c = await self._wait_approval(sessions, context, run_c)
        # 同内容（b/c）必须同 digest：身份（run_id）派生的 digest 使 swap 检测
        # 结构上不可触发——内容绑定是 spec §4 增补的契约
        assert approvals_b[0].input_digest == approvals_c[0].input_digest, (
            "同节点内容必须产生相同 digest（当前为 run 身份派生）"
        )
        assert approvals_a[0].input_digest != approvals_b[0].input_digest, (
            "digest 必须随节点声明内容变化"
        )
        for handle in (handle_a, handle_b, handle_c):
            await handle.signal("cancel", {"command_event_id": str(new_id())})
            await asyncio.wait_for(handle.result(), timeout=60)

    async def test_conflict_lands_as_event_and_synthesize_degrades(
        self, temporal_env, worker_stack
    ) -> None:
        """K-1 并行冲突：ConflictDetected 落 canonical event；Synthesize 降级。"""
        from zhiwei.agents.task_graph import MergeStrategy
        from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput

        class _ConflictFixtureHandler(TaskHandler):
            """输出图声明的 conflict 字段（conftest 的 Fixture 只输出
            task_id——声明字段与输出不符时冲突路径根本不会被走到）。"""

            @property
            def primitive_type(self) -> str:
                return "ConflictFixture"

            @property
            def handler_version(self) -> int:
                return 1

            def execute(self, input: TaskInput) -> TaskOutput:
                return TaskOutput(output_values={"decision": f"value-{input.task_id}"})

        def _conflict_node(task_id: str) -> TaskGraphNode:
            return TaskGraphNode(
                task_id=task_id,
                task_type="ConflictFixture",
                dependencies=(),
                parallel_safe=True,
                required_capability="fixture",
                output_schema={"decision": {"type": "string"}},
                output_merge_strategies={"decision": MergeStrategy.CONFLICT_PRESERVING},
            )

        graph = TaskGraph(
            nodes={
                "a": _conflict_node("a"),
                "b": _conflict_node("b"),
                "synth": TaskGraphNode(
                    task_id="synth",
                    task_type="Synthesize",
                    dependencies=("a", "b"),
                    parallel_safe=False,
                    required_capability="fixture",
                ),
            },
            edges={"synth": ["a", "b"]},
        )
        _, sessions, context, registry = worker_stack
        # registry 完整性（批次 C 预检契约）：本场景 handler 必须注册
        from zhiwei.runtime.handlers.core import SynthesizeHandler

        if not registry.has_handler("Synthesize"):
            registry.register(SynthesizeHandler())
        if not registry.has_handler("ConflictFixture"):
            registry.register(_ConflictFixtureHandler())
        run_id, handle = await self._start(temporal_env, sessions, context, graph)
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result.status == "completed"

        async with tenant_session(sessions, context) as session:
            store = RuntimeEventStore(session, context)
            state = await store.reduce_state(uuid_module.UUID(run_id))
            events = await store.load_events(uuid_module.UUID(run_id))
        types = [type(e).__name__ for e in events]
        assert "ConflictDetected" in types, (
            "冲突必须作为 canonical event 落账（重放后仍在，spec §3 增补）"
        )
        assert len(state.conflicts) >= 1
        synth_output = state.tasks["synth"].output_values
        assert synth_output.get("synthesize_downgraded") is True, (
            f"存在未解决冲突时 Synthesize 必须降级（实际输出 {synth_output}）"
        )
