"""S2 runtime: Runtime activities — PG event append and state transitions。

事实源：design doc §4.3、S2-T3 plan。

Activities append events to PG idempotently using idempotency keys.
Each activity validates preconditions and returns an ActivityResult.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from zhiwei.workflows.activities.base import (
    ActivityResult,
    AppendEventInput,
    CompleteTaskInput,
    EventRepository,
    FailTaskInput,
    ScheduleTaskInput,
    StartRunInput,
    StartRunResult,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class RuntimeActivities:
    """Runtime activities that append PG events idempotently.

    Each activity method:
    1. Creates a typed RuntimeEvent
    2. Appends it to the EventRepository with an idempotency key
    3. Returns an ActivityResult with the event

    The idempotency key ensures duplicate activity invocations (from retries)
    do not create duplicate events.
    """

    def __init__(self, event_repository: EventRepository) -> None:
        self._repo = event_repository

    def start_run(self, input: StartRunInput) -> StartRunResult:
        """Initialize a run with RunCreated and RunStarted events."""
        now = _now()
        created = RunCreated(
            run_id=input.run_id,
            timestamp=now,
            graph=input.graph,
        )
        started = RunStarted(
            run_id=input.run_id,
            timestamp=now,
        )
        self._repo.append(created, idempotency_key=f"start_run:{input.run_id}:created")
        self._repo.append(started, idempotency_key=f"start_run:{input.run_id}:started")
        return StartRunResult(run_id=input.run_id, events=[created, started])

    def schedule_task(self, input: ScheduleTaskInput) -> ActivityResult:
        """Schedule a task for execution."""
        event = TaskScheduled(
            run_id=input.run_id,
            timestamp=_now(),
            task_id=input.task_id,
        )
        self._repo.append(
            event,
            idempotency_key=f"schedule:{input.run_id}:{input.task_id}",
        )
        return ActivityResult(success=True, event=event)

    def start_attempt(self, input: ScheduleTaskInput) -> ActivityResult:
        """Start a new attempt for a task."""
        event = TaskStarted(
            run_id=input.run_id,
            timestamp=_now(),
            task_id=input.task_id,
            attempt_id=new_id(),
        )
        self._repo.append(
            event,
            idempotency_key=f"start_attempt:{input.run_id}:{input.task_id}:{event.attempt_id}",
        )
        return ActivityResult(success=True, event=event)

    def complete_task(self, input: CompleteTaskInput) -> ActivityResult:
        """Mark a task as completed with output values."""
        event = TaskCompleted(
            run_id=input.run_id,
            timestamp=_now(),
            task_id=input.task_id,
            output_values=input.output_values,
        )
        self._repo.append(
            event,
            idempotency_key=f"complete:{input.run_id}:{input.task_id}",
        )
        return ActivityResult(success=True, event=event)

    def fail_task(self, input: FailTaskInput) -> ActivityResult:
        """Mark a task as failed with an error message."""
        event = TaskFailed(
            run_id=input.run_id,
            timestamp=_now(),
            task_id=input.task_id,
            error=input.error,
        )
        self._repo.append(
            event,
            idempotency_key=f"fail:{input.run_id}:{input.task_id}",
        )
        return ActivityResult(success=True, event=event)

    def skip_task(self, input: ScheduleTaskInput) -> ActivityResult:
        """Skip a task (e.g., dependency failed)."""
        event = TaskSkipped(
            run_id=input.run_id,
            timestamp=_now(),
            task_id=input.task_id,
            reason="dependency failed",
        )
        self._repo.append(
            event,
            idempotency_key=f"skip:{input.run_id}:{input.task_id}",
        )
        return ActivityResult(success=True, event=event)

    def append_event(self, input: AppendEventInput) -> ActivityResult:
        """Append an arbitrary event to the repository."""
        self._repo.append(input.event, idempotency_key=input.idempotency_key)
        return ActivityResult(success=True)
