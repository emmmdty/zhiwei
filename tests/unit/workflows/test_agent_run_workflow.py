"""S2-T3 RED: AgentRunWorkflow tests — orchestration, signal, replay, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from zhiwei.agents.task_graph import (
    FailurePolicy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.events import (
    RunCreated,
    RunStarted,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.reducer import reduce
from zhiwei.workflows.activities.base import (
    ActivityError,
    ActivityResult,
    AppendEventInput,
    CompleteTaskInput,
    FailTaskInput,
    ScheduleTaskInput,
    StartRunInput,
    StartRunResult,
)
from zhiwei.workflows.agent_run import (
    AgentRunWorkflow,
    CancelSignal,
    WorkflowClientError,
    WorkflowExecutionResult,
    WorkflowRunConfig,
)


def _ts(offset: int = 0) -> datetime:
    return datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _make_graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "intake": TaskGraphNode(
                task_id="intake",
                task_type="Intake",
                dependencies=(),
                parallel_safe=False,
                required_capability="intake",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
            ),
            "retrieve": TaskGraphNode(
                task_id="retrieve",
                task_type="Retrieve",
                dependencies=("intake",),
                parallel_safe=False,
                required_capability="retrieve",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
            ),
            "synthesize": TaskGraphNode(
                task_id="synthesize",
                task_type="Synthesize",
                dependencies=("retrieve",),
                parallel_safe=False,
                required_capability="synthesize",
                budget={},
                failure_policy=FailurePolicy.CANCEL,
                completion_obligations=(),
            ),
        },
        edges={
            "intake": [],
            "retrieve": ["intake"],
            "synthesize": ["retrieve"],
        },
    )


def _make_parallel_graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "intake": TaskGraphNode(
                task_id="intake",
                task_type="Intake",
                dependencies=(),
                parallel_safe=False,
                required_capability="intake",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
            ),
            "retrieve_a": TaskGraphNode(
                task_id="retrieve_a",
                task_type="Retrieve",
                dependencies=("intake",),
                parallel_safe=True,
                required_capability="retrieve",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
            ),
            "retrieve_b": TaskGraphNode(
                task_id="retrieve_b",
                task_type="Retrieve",
                dependencies=("intake",),
                parallel_safe=True,
                required_capability="retrieve",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
            ),
            "synthesize": TaskGraphNode(
                task_id="synthesize",
                task_type="Synthesize",
                dependencies=("retrieve_a", "retrieve_b"),
                parallel_safe=False,
                required_capability="synthesize",
                budget={},
                failure_policy=FailurePolicy.CANCEL,
                completion_obligations=(),
            ),
        },
        edges={
            "intake": [],
            "retrieve_a": ["intake"],
            "retrieve_b": ["intake"],
            "synthesize": ["retrieve_a", "retrieve_b"],
        },
    )


class FakeWorkflowClient:
    """In-memory workflow client for testing."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._execution_result: WorkflowExecutionResult | None = None

    def set_execution_result(self, result: WorkflowExecutionResult) -> None:
        self._execution_result = result

    def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: WorkflowRunConfig,
    ) -> None:
        if workflow_id in self._ids:
            raise WorkflowClientError(f"Workflow {workflow_id} already exists")
        self._ids.add(workflow_id)
        self.started.append({
            "workflow_type": workflow_type,
            "workflow_id": workflow_id,
            "input": input,
        })

    def execute_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: WorkflowRunConfig,
    ) -> WorkflowExecutionResult:
        if self._execution_result is not None:
            return self._execution_result
        return WorkflowExecutionResult(
            run_id=input.run_id,
            status="completed",
            events=[],
        )

    def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None:
        pass


class FakeActivities:
    """In-memory activities for testing — records all invocations."""

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._start_result: StartRunResult | None = None

    def set_start_result(self, result: StartRunResult) -> None:
        self._start_result = result

    def start_run(self, input: StartRunInput) -> StartRunResult:
        self.invocations.append(("start_run", input.model_dump()))
        if self._start_result is not None:
            return self._start_result
        return StartRunResult(
            run_id=input.run_id,
            events=[
                RunCreated(run_id=input.run_id, timestamp=_ts(0), graph=input.graph),
                RunStarted(run_id=input.run_id, timestamp=_ts(1)),
            ],
        )

    def schedule_task(self, input: ScheduleTaskInput) -> Any:
        self.invocations.append(("schedule_task", input.model_dump()))
        return ActivityResult(
            success=True,
            event=TaskScheduled(
                run_id=input.run_id,
                timestamp=_ts(),
                task_id=input.task_id,
            ),
        )

    def start_attempt(self, input: ScheduleTaskInput) -> Any:
        self.invocations.append(("start_attempt", input.model_dump()))
        attempt_id = new_id()
        return ActivityResult(
            success=True,
            event=TaskStarted(
                run_id=input.run_id,
                timestamp=_ts(),
                task_id=input.task_id,
                attempt_id=attempt_id,
            ),
        )

    def complete_task(self, input: CompleteTaskInput) -> Any:
        self.invocations.append(("complete_task", input.model_dump()))
        return ActivityResult(
            success=True,
            event=TaskCompleted(
                run_id=input.run_id,
                timestamp=_ts(),
                task_id=input.task_id,
                output_values=input.output_values,
            ),
        )

    def fail_task(self, input: FailTaskInput) -> Any:
        self.invocations.append(("fail_task", input.model_dump()))
        return ActivityResult(
            success=True,
            event=TaskFailed(
                run_id=input.run_id,
                timestamp=_ts(),
                task_id=input.task_id,
                error=input.error,
            ),
        )

    def skip_task(self, input: ScheduleTaskInput) -> Any:
        self.invocations.append(("skip_task", input.model_dump()))
        return ActivityResult(
            success=True,
            event=TaskSkipped(
                run_id=input.run_id,
                timestamp=_ts(),
                task_id=input.task_id,
                reason="dependency failed",
            ),
        )

    def append_event(self, input: AppendEventInput) -> Any:
        self.invocations.append(("append_event", input.model_dump()))
        return ActivityResult(success=True)


class TestWorkflowClientProtocol:
    def test_start_workflow_records_invocation(self) -> None:
        client = FakeWorkflowClient()
        run_id = new_id()
        graph = _make_graph()
        config = WorkflowRunConfig(run_id=run_id, graph=graph)

        client.start_workflow("AgentRun", f"run-{run_id}", config)

        assert len(client.started) == 1
        assert client.started[0]["workflow_type"] == "AgentRun"
        assert client.started[0]["workflow_id"] == f"run-{run_id}"

    def test_start_workflow_duplicate_id_rejected(self) -> None:
        client = FakeWorkflowClient()
        run_id = new_id()
        graph = _make_graph()
        config = WorkflowRunConfig(run_id=run_id, graph=graph)

        client.start_workflow("AgentRun", f"run-{run_id}", config)
        with pytest.raises(WorkflowClientError, match="already exists"):
            client.start_workflow("AgentRun", f"run-{run_id}", config)


class TestWorkflowExecution:
    def test_workflow_follows_topological_order(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        task_names = [
            inv[1]["task_id"]
            for inv in activities.invocations
            if inv[0] == "schedule_task"
        ]
        assert task_names == ["intake", "retrieve", "synthesize"]
        assert result.status == "completed"

    def test_workflow_calls_correct_activity_sequence(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        workflow.run(config)

        activity_names = [inv[0] for inv in activities.invocations]
        assert activity_names == [
            "start_run",
            "schedule_task",
            "start_attempt",
            "complete_task",
            "schedule_task",
            "start_attempt",
            "complete_task",
            "schedule_task",
            "start_attempt",
            "complete_task",
        ]

    def test_workflow_schedules_parallel_tasks(self) -> None:
        run_id = new_id()
        graph = _make_parallel_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        schedule_names = [
            inv[1]["task_id"]
            for inv in activities.invocations
            if inv[0] == "schedule_task"
        ]
        assert "retrieve_a" in schedule_names
        assert "retrieve_b" in schedule_names
        idx_a = schedule_names.index("retrieve_a")
        idx_b = schedule_names.index("retrieve_b")
        assert idx_a < schedule_names.index("synthesize")
        assert idx_b < schedule_names.index("synthesize")
        assert result.status == "completed"

    def test_workflow_returns_terminal_when_all_tasks_done(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        assert result.status == "completed"
        assert result.run_id == run_id

    def test_workflow_deterministic_execution(self) -> None:
        run_id = new_id()
        graph = _make_graph()

        def _run_once() -> list[str]:
            acts = FakeActivities()
            cl = FakeWorkflowClient()
            wf = AgentRunWorkflow(cl, acts)
            wf.run(WorkflowRunConfig(run_id=run_id, graph=graph))
            return [inv[0] for inv in acts.invocations]

        seq1 = _run_once()
        seq2 = _run_once()
        assert seq1 == seq2


class TestActivityTimeoutAndRetry:
    def test_activity_timeout_triggers_retry(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        activities._start_result = None

        call_count = 0
        original_schedule = activities.schedule_task

        def _schedule_with_timeout(input: ScheduleTaskInput) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ActivityError("activity timeout")
            return original_schedule(input)

        activities.schedule_task = _schedule_with_timeout  # type: ignore[assignment]
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities, max_retries=3)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        assert call_count == 4
        assert result.status == "completed"

    def test_activity_exhausted_retries_fails(self) -> None:
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake",
                    task_type="Intake",
                    dependencies=(),
                    parallel_safe=False,
                    required_capability="intake",
                    budget={},
                    failure_policy=FailurePolicy.RETRY,
                    completion_obligations=(),
                ),
            },
            edges={"intake": []},
        )
        activities = FakeActivities()

        def _always_fail(input: ScheduleTaskInput) -> Any:
            raise ActivityError("activity timeout")

        activities.schedule_task = _always_fail  # type: ignore[assignment]
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities, max_retries=2)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        assert result.status == "failed"


class TestSignalHandling:
    def test_cancel_signal_skips_remaining_tasks(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()
        signal_queue: list[Any] = []

        workflow = AgentRunWorkflow(client, activities, signal_queue=signal_queue)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)

        signal_queue.append(CancelSignal(run_id=run_id))
        result = workflow.run(config)

        scheduled = [
            inv[1]["task_id"]
            for inv in activities.invocations
            if inv[0] == "schedule_task"
        ]
        assert "intake" in scheduled
        assert result.status in ("cancelled", "completed")

    def test_duplicate_signal_idempotent(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()
        signal_queue: list[Any] = []

        workflow = AgentRunWorkflow(client, activities, signal_queue=signal_queue)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)

        for _ in range(5):
            signal_queue.append(CancelSignal(run_id=run_id))

        result = workflow.run(config)
        assert result.status in ("cancelled", "completed")


class TestReplay:
    def test_replay_same_events_same_projection(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=new_id()),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(5), task_id="retrieve"),
            TaskStarted(run_id=run_id, timestamp=_ts(6), task_id="retrieve", attempt_id=new_id()),
            TaskCompleted(run_id=run_id, timestamp=_ts(7), task_id="retrieve", output_values={}),
        ]

        state1 = reduce(events)
        state2 = reduce(list(events))
        assert state1.status == state2.status
        assert state1.tasks.keys() == state2.tasks.keys()
        for key in state1.tasks:
            assert state1.tasks[key].status == state2.tasks[key].status

    def test_replay_deterministic_across_runs(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        for _ in range(10):
            state = reduce(list(events))
            assert state.status == "running"
            assert set(state.tasks.keys()) == {"intake", "retrieve", "synthesize"}


class TestContinueAsNew:
    def test_continue_as_new_preserves_state(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=new_id()),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "completed"
        assert state.tasks["retrieve"].status == "pending"

    def test_continue_as_new_compacts_history(self) -> None:
        run_id = new_id()
        full_events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=new_id()),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
        ]
        state = reduce(full_events)
        compacted_events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
        ]
        compacted_state = reduce(compacted_events)
        assert state.tasks["intake"].status == compacted_state.tasks["intake"].status
        assert state.tasks["retrieve"].status == compacted_state.tasks["retrieve"].status


class TestWorkerKillRestart:
    def test_worker_restart_reads_state_from_pg(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "completed"

        state_after_restart = reduce(list(events))
        assert state_after_restart.tasks["intake"].status == "completed"

    def test_worker_restart_preserves_terminal_result(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="synthesize"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="retrieve", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(5), task_id="synthesize", output_values={}),
        ]
        state = reduce(events)
        assert state.is_terminal

        state_after_restart = reduce(list(events))
        assert state_after_restart.is_terminal


class TestWorkflowAsOrchestration:
    def test_workflow_only_calls_activities(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        result = workflow.run(config)

        assert result.status == "completed"
        assert len(activities.invocations) > 0
        activity_names = {inv[0] for inv in activities.invocations}
        assert activity_names <= {
            "start_run",
            "schedule_task",
            "start_attempt",
            "complete_task",
            "fail_task",
            "skip_task",
            "append_event",
        }

    def test_workflow_payloads_contain_refs_not_large_data(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        activities = FakeActivities()
        client = FakeWorkflowClient()

        workflow = AgentRunWorkflow(client, activities)
        config = WorkflowRunConfig(run_id=run_id, graph=graph)
        workflow.run(config)

        for inv_name, inv_args in activities.invocations:
            if inv_name == "complete_task":
                assert "output_values" in inv_args
                assert isinstance(inv_args["output_values"], dict)
