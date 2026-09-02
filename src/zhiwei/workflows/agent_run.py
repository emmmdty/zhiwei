"""S2 runtime: Temporal workflow for agent runs。

事实源：design doc §4.3、S2-T3 plan。

Workflow is orchestration only — it determines task ordering and calls activities.
PG events are the source of truth; workflow history only stores refs.
No Temporal SDK dependency — WorkflowClient protocol abstracts the integration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.agents.task_graph import TaskGraph
from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.events import (
    RuntimeEvent,
    TaskScheduled,
    TaskStarted,
)
from zhiwei.runtime.reducer import RunState, reduce
from zhiwei.workflows.activities.base import (
    Activities,
    ActivityError,
    CompleteTaskInput,
    FailTaskInput,
    ScheduleTaskInput,
    StartRunInput,
)


class WorkflowClientError(RuntimeError):
    """Workflow client operation failed (duplicate ID, not found)."""


class WorkflowRunConfig(BaseModel):
    """Configuration for starting an agent run workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    graph: TaskGraph
    max_attempts: int = Field(default=3, ge=1)


class WorkflowExecutionResult(BaseModel):
    """Result of a workflow execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class CancelSignal(BaseModel):
    """Signal to cancel a running workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class PauseSignal(BaseModel):
    """Signal to pause a running workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class WorkflowClient(Protocol):
    """Port for the Temporal workflow client.

    Abstracts the Temporal SDK integration. Implementations handle
    workflow start, signal, and execution.
    """

    def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: WorkflowRunConfig,
    ) -> None: ...

    def execute_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        input: WorkflowRunConfig,
    ) -> WorkflowExecutionResult: ...

    def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None: ...


class AgentRunWorkflow:
    """Orchestration workflow for agent runs.

    Determines task scheduling order using topological sort and delegates
    all side effects to activities. PG events are the source of truth.

    The workflow:
    1. Starts the run via start_run activity
    2. Schedules tasks in topological order
    3. Starts attempts and waits for completion/failure
    4. Skips downstream tasks when dependencies fail
    5. Handles cancel/pause signals
    6. Returns terminal state when all tasks are done
    """

    WORKFLOW_TYPE = "AgentRun"

    def __init__(
        self,
        client: WorkflowClient,
        activities: Activities,
        *,
        max_retries: int = 3,
        signal_queue: list[Any] | None = None,
    ) -> None:
        self._client = client
        self._activities = activities
        self._max_retries = max_retries
        self._signal_queue = signal_queue or []

    def start(self, config: WorkflowRunConfig) -> None:
        """Start the workflow via the client."""
        workflow_id = f"run-{config.run_id}"
        self._client.start_workflow(
            workflow_type=self.WORKFLOW_TYPE,
            workflow_id=workflow_id,
            input=config,
        )

    def run(self, config: WorkflowRunConfig) -> WorkflowExecutionResult:
        """Execute the workflow orchestration loop.

        Schedules tasks in topological order, calling activities for each
        state transition. Tracks events directly and reduces them for state.
        """
        events: list[RuntimeEvent] = []
        start_result = self._activities.start_run(
            StartRunInput(run_id=config.run_id, graph=config.graph)
        )
        events.extend(start_result.events)
        state = reduce(events)

        completed: set[str] = set()
        cancelled = False

        while not state.is_terminal and not cancelled:
            cancelled = self._process_signals(config.run_id)
            if cancelled:
                break

            ready = config.graph.ready_tasks(completed)
            if not ready:
                break

            for task_id in sorted(ready):
                task_state = state.tasks.get(task_id)
                if task_state is None or task_state.status != "pending":
                    continue

                if self._has_failed_dependencies(task_id, config.graph, state):
                    skip_result = self._activities.skip_task(
                        ScheduleTaskInput(run_id=config.run_id, task_id=task_id)
                    )
                    if skip_result.event is not None:
                        events.append(skip_result.event)
                        state = reduce(events)
                    completed.add(task_id)
                    continue

                task_events = self._execute_task(config, task_id, events)
                events.extend(task_events)
                state = reduce(events)
                completed.add(task_id)

        if cancelled:
            status = "cancelled"
        elif state.is_terminal:
            has_failure = any(
                t.status == "failed" for t in state.tasks.values()
            )
            status = "failed" if has_failure else "completed"
        else:
            status = "running"
        return WorkflowExecutionResult(
            run_id=config.run_id,
            status=status,
            events=[],
        )

    def _execute_task(
        self,
        config: WorkflowRunConfig,
        task_id: str,
        existing_events: list[RuntimeEvent],
    ) -> list[RuntimeEvent]:
        """Execute a single task through schedule -> attempt -> complete.

        Returns the list of events generated during task execution.
        On ActivityError exhaustion, emits prerequisite events then fails.
        """
        task_events: list[RuntimeEvent] = []

        try:
            schedule_result = self._invoke_with_retry(
                lambda: self._activities.schedule_task(
                    ScheduleTaskInput(run_id=config.run_id, task_id=task_id)
                ),
            )
            if schedule_result.event is not None:
                task_events.append(schedule_result.event)

            attempt_result = self._invoke_with_retry(
                lambda: self._activities.start_attempt(
                    ScheduleTaskInput(run_id=config.run_id, task_id=task_id)
                ),
            )
            if attempt_result.event is not None:
                task_events.append(attempt_result.event)

            complete_result = self._invoke_with_retry(
                lambda: self._activities.complete_task(
                    CompleteTaskInput(
                        run_id=config.run_id,
                        task_id=task_id,
                        output_values={},
                    )
                ),
            )
            if complete_result.event is not None:
                task_events.append(complete_result.event)

        except ActivityError:
            now = datetime.now(tz=UTC)
            task_events.append(TaskScheduled(
                run_id=config.run_id, timestamp=now, task_id=task_id,
            ))
            task_events.append(TaskStarted(
                run_id=config.run_id, timestamp=now,
                task_id=task_id, attempt_id=new_id(),
            ))
            fail_result = self._activities.fail_task(
                FailTaskInput(
                    run_id=config.run_id,
                    task_id=task_id,
                    error="activity retries exhausted",
                )
            )
            if fail_result.event is not None:
                task_events.append(fail_result.event)

        return task_events

    def _process_signals(self, run_id: UUID) -> bool:
        """Process pending signals. Returns True if workflow should stop."""
        processed: list[Any] = []
        should_stop = False
        for signal in self._signal_queue:
            if isinstance(signal, CancelSignal) and signal.run_id == run_id:
                should_stop = True
            processed.append(signal)
        self._signal_queue.clear()
        self._signal_queue.extend(processed)
        return should_stop

    def _invoke_with_retry(self, fn: Any) -> Any:
        """Invoke an activity function with retry on ActivityError."""
        last_error: ActivityError | None = None
        for _attempt in range(self._max_retries):
            try:
                return fn()
            except ActivityError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise ActivityError("retry loop completed without result")

    def _has_failed_dependencies(
        self,
        task_id: str,
        graph: TaskGraph,
        state: RunState,
    ) -> bool:
        """Check if any dependency of a task has failed or been skipped."""
        deps = graph.edges.get(task_id, [])
        for dep_id in deps:
            dep_state = state.tasks.get(dep_id)
            if dep_state is not None and dep_state.status in ("failed", "skipped"):
                return True
        return False
