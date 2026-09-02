"""S2 runtime: Activity base classes and port definitions。

事实源：design doc §4.3、S2-T3 plan、ADR-005。

Activities are the side-effect boundary. Ports (Protocol classes) define the
contract between workflow orchestration and activity implementations.
No Temporal SDK dependency — protocols abstract the Temporal integration.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.agents.task_graph import TaskGraph
from zhiwei.runtime.events import RuntimeEvent


class ActivityError(RuntimeError):
    """Activity execution failed (timeout, retryable error)."""


class EventRepositoryError(RuntimeError):
    """Event repository operation failed (duplicate key, constraint violation)."""


class StartRunInput(BaseModel):
    """Input for the start_run activity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    graph: TaskGraph


class StartRunResult(BaseModel):
    """Result from the start_run activity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    events: list[RuntimeEvent]


class ScheduleTaskInput(BaseModel):
    """Input for scheduling or starting a task attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: str


class CompleteTaskInput(BaseModel):
    """Input for completing a task with output values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: str
    output_values: dict[str, Any] = Field(default_factory=dict)


class FailTaskInput(BaseModel):
    """Input for failing a task with an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: str
    error: str


class AppendEventInput(BaseModel):
    """Input for appending an event to the repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: RuntimeEvent
    idempotency_key: str


class ActivityResult(BaseModel):
    """Generic result from an activity invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    event: RuntimeEvent | None = None
    error: str | None = None


class EventRepository(Protocol):
    """Port for persisting runtime events with idempotency.

    Implementations must reject duplicate idempotency keys.
    """

    def append(self, event: RuntimeEvent, *, idempotency_key: str) -> None: ...

    def get_events(self, run_id: UUID) -> list[RuntimeEvent]: ...


class Activities(Protocol):
    """Protocol defining all activities available to workflow orchestration.

    Each method corresponds to a Temporal activity. Implementations handle
    the actual side effects (PG event append, state transitions).
    """

    def start_run(self, input: StartRunInput) -> StartRunResult: ...

    def schedule_task(self, input: ScheduleTaskInput) -> ActivityResult: ...

    def start_attempt(self, input: ScheduleTaskInput) -> ActivityResult: ...

    def complete_task(self, input: CompleteTaskInput) -> ActivityResult: ...

    def fail_task(self, input: FailTaskInput) -> ActivityResult: ...

    def skip_task(self, input: ScheduleTaskInput) -> ActivityResult: ...

    def append_event(self, input: AppendEventInput) -> ActivityResult: ...
