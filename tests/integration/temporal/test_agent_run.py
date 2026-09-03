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

        registry = TaskHandlerRegistry()
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
