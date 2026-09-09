"""S2 runtime: event types for run lifecycle and task state transitions。

事实源：design doc §4.3、S2-T2 plan。

Events are the source of truth for run state. The reducer replays events into a Run projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.agents.task_graph import TaskGraph
from zhiwei.contracts.identifiers import new_id


class RuntimeEvent(BaseModel):
    """Base class for all runtime events.

    Each event carries a run_id, event_id for idempotency, and a timestamp.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    event_id: UUID = Field(default_factory=new_id)
    timestamp: datetime


class RunCreated(RuntimeEvent):
    """A new run has been created with its task graph."""

    graph: TaskGraph


class RunStarted(RuntimeEvent):
    """The run has been started and is now executing."""


class RunCompleted(RuntimeEvent):
    """All tasks reached a terminal state and the run completed."""


class RunFailed(RuntimeEvent):
    """The run ended in failure (at least one task failed terminally)."""

    error: str


class RunCancelled(RuntimeEvent):
    """The run was cancelled; no new tasks are dispatched after this point."""

    reason: str | None = None


class RunPaused(RuntimeEvent):
    """The run was paused (backpressure / operator action)."""

    reason: str | None = None


class RunResumed(RuntimeEvent):
    """A paused run was resumed."""


class TaskScheduled(RuntimeEvent):
    """A task has been scheduled and is waiting to start."""

    task_id: str


class TaskStarted(RuntimeEvent):
    """A task has started execution with a new attempt."""

    task_id: str
    attempt_id: UUID


class TaskCompleted(RuntimeEvent):
    """A task has completed successfully with output values."""

    task_id: str
    output_values: dict[str, Any] = Field(default_factory=dict)


class TaskFailed(RuntimeEvent):
    """A task has failed with an error message."""

    task_id: str
    error: str
    attempt_id: UUID | None = None


class TaskSkipped(RuntimeEvent):
    """A task has been skipped (e.g., dependency failed)."""

    task_id: str
    reason: str


class AttemptCreated(RuntimeEvent):
    """A new attempt has been created for a task."""

    task_id: str
    attempt_id: UUID
    attempt_number: int


class AttemptCommitted(RuntimeEvent):
    """An attempt has been committed (successfully completed)."""

    task_id: str
    attempt_id: UUID


class AttemptAborted(RuntimeEvent):
    """An attempt has been aborted (failed or cancelled)."""

    task_id: str
    attempt_id: UUID


class ConflictDetected(RuntimeEvent):
    """A parallel merge conflict has been detected on a canonical state field."""

    field: str
    values: dict[str, Any]
    conflict_record: dict[str, Any]


RuntimeEventUnion = (
    RunCreated
    | RunStarted
    | RunCompleted
    | RunFailed
    | RunCancelled
    | RunPaused
    | RunResumed
    | TaskScheduled
    | TaskStarted
    | TaskCompleted
    | TaskFailed
    | TaskSkipped
    | AttemptCreated
    | AttemptCommitted
    | AttemptAborted
    | ConflictDetected
)
