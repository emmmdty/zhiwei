"""S2-T3 RED: Activities tests — idempotent event append, state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from zhiwei.agents.task_graph import (
    FailurePolicy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.events import (
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.workflows.activities.base import (
    ActivityError,
    AppendEventInput,
    CompleteTaskInput,
    EventRepositoryError,
    FailTaskInput,
    ScheduleTaskInput,
)
from zhiwei.workflows.activities.runtime import RuntimeActivities


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
        },
        edges={
            "intake": [],
            "retrieve": ["intake"],
        },
    )


class InMemoryEventRepository:
    """In-memory event repository for testing."""

    def __init__(self) -> None:
        self.events: list = []
        self._idempotency_keys: set[str] = set()

    def append(self, event: object, *, idempotency_key: str) -> None:
        if idempotency_key in self._idempotency_keys:
            raise EventRepositoryError(
                f"Duplicate idempotency key: {idempotency_key}"
            )
        self._idempotency_keys.add(idempotency_key)
        self.events.append(event)

    def get_events(self, run_id: UUID) -> list:
        return [e for e in self.events if e.run_id == run_id]


class TestEventRepositoryIdempotency:
    def test_append_event_with_unique_key_succeeds(self) -> None:
        repo = InMemoryEventRepository()
        run_id = new_id()
        event = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")

        repo.append(event, idempotency_key="key-1")
        assert len(repo.events) == 1

    def test_append_event_duplicate_key_rejected(self) -> None:
        repo = InMemoryEventRepository()
        run_id = new_id()
        event1 = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")
        event2 = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="retrieve")

        repo.append(event1, idempotency_key="key-1")
        with pytest.raises(EventRepositoryError, match="Duplicate idempotency key"):
            repo.append(event2, idempotency_key="key-1")

    def test_append_event_same_content_different_key_succeeds(self) -> None:
        repo = InMemoryEventRepository()
        run_id = new_id()
        event1 = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")
        event2 = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")

        repo.append(event1, idempotency_key="key-1")
        repo.append(event2, idempotency_key="key-2")
        assert len(repo.events) == 2

    def test_events_filtered_by_run_id(self) -> None:
        repo = InMemoryEventRepository()
        run1 = new_id()
        run2 = new_id()

        repo.append(
            TaskScheduled(run_id=run1, timestamp=_ts(), task_id="intake"),
            idempotency_key="k1",
        )
        repo.append(
            TaskScheduled(run_id=run2, timestamp=_ts(), task_id="intake"),
            idempotency_key="k2",
        )

        assert len(repo.get_events(run1)) == 1
        assert len(repo.get_events(run2)) == 1


class TestRuntimeActivitiesAppendEvent:
    def test_append_event_persists_to_repository(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()
        event = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")

        result = activities.append_event(
            AppendEventInput(event=event, idempotency_key="ik-1")
        )

        assert result.success is True
        assert len(repo.events) == 1

    def test_append_event_idempotent_second_call_rejected(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()
        event = TaskScheduled(run_id=run_id, timestamp=_ts(), task_id="intake")

        activities.append_event(AppendEventInput(event=event, idempotency_key="ik-1"))
        with pytest.raises(EventRepositoryError):
            activities.append_event(AppendEventInput(event=event, idempotency_key="ik-1"))


class TestRuntimeActivitiesStateTransitions:
    def test_schedule_task_transitions_pending_to_scheduled(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        result = activities.schedule_task(
            ScheduleTaskInput(run_id=run_id, task_id="intake")
        )

        assert result.success is True
        assert result.event is not None
        assert isinstance(result.event, TaskScheduled)
        assert result.event.task_id == "intake"

    def test_start_attempt_creates_task_started_event(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        result = activities.start_attempt(
            ScheduleTaskInput(run_id=run_id, task_id="intake")
        )

        assert result.success is True
        assert result.event is not None
        assert isinstance(result.event, TaskStarted)
        assert result.event.task_id == "intake"

    def test_complete_task_creates_task_completed_event(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        result = activities.complete_task(
            CompleteTaskInput(
                run_id=run_id,
                task_id="intake",
                output_values={"key": "value"},
            )
        )

        assert result.success is True
        assert result.event is not None
        assert isinstance(result.event, TaskCompleted)
        assert result.event.output_values == {"key": "value"}

    def test_fail_task_creates_task_failed_event(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        result = activities.fail_task(
            FailTaskInput(
                run_id=run_id,
                task_id="intake",
                error="timeout",
            )
        )

        assert result.success is True
        assert result.event is not None
        assert isinstance(result.event, TaskFailed)
        assert result.event.error == "timeout"

    def test_skip_task_creates_task_skipped_event(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        result = activities.skip_task(
            ScheduleTaskInput(run_id=run_id, task_id="intake")
        )

        assert result.success is True
        assert result.event is not None
        assert isinstance(result.event, TaskSkipped)
        assert result.event.reason == "dependency failed"


class TestRuntimeActivitiesEventAppend:
    def test_all_activity_events_persisted(self) -> None:
        repo = InMemoryEventRepository()
        activities = RuntimeActivities(repo)
        run_id = new_id()

        activities.schedule_task(ScheduleTaskInput(run_id=run_id, task_id="intake"))
        activities.start_attempt(ScheduleTaskInput(run_id=run_id, task_id="intake"))
        activities.complete_task(
            CompleteTaskInput(run_id=run_id, task_id="intake", output_values={})
        )

        events = repo.get_events(run_id)
        assert len(events) == 3
        assert isinstance(events[0], TaskScheduled)
        assert isinstance(events[1], TaskStarted)
        assert isinstance(events[2], TaskCompleted)

    def test_events_are_deterministic(self) -> None:
        run_id = new_id()

        def _run() -> list:
            repo = InMemoryEventRepository()
            activities = RuntimeActivities(repo)
            activities.schedule_task(ScheduleTaskInput(run_id=run_id, task_id="intake"))
            activities.start_attempt(ScheduleTaskInput(run_id=run_id, task_id="intake"))
            activities.complete_task(
                CompleteTaskInput(run_id=run_id, task_id="intake", output_values={})
            )
            return repo.get_events(run_id)

        events1 = _run()
        events2 = _run()
        assert len(events1) == len(events2)
        for e1, e2 in zip(events1, events2, strict=True):
            assert type(e1) is type(e2)
            assert e1.task_id == e2.task_id


class TestActivityError:
    def test_activity_error_is_runtime_error(self) -> None:
        error = ActivityError("test error")
        assert isinstance(error, RuntimeError)
        assert str(error) == "test error"

    def test_event_repository_error_is_runtime_error(self) -> None:
        error = EventRepositoryError("test error")
        assert isinstance(error, RuntimeError)
        assert str(error) == "test error"
