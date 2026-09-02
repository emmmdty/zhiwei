"""S2 runtime: Activity implementations for agent workflows。

事实源：design doc §4.3、S2-T3 plan。

Activities are the side-effect boundary — they append PG events idempotently.
Workflow orchestration calls activities; activities never call back into workflow.
"""

from __future__ import annotations

from zhiwei.workflows.activities.base import (
    ActivityError,
    ActivityResult,
    AppendEventInput,
    CompleteTaskInput,
    EventRepository,
    EventRepositoryError,
    FailTaskInput,
    ScheduleTaskInput,
    StartRunInput,
    StartRunResult,
)
from zhiwei.workflows.activities.runtime import RuntimeActivities

__all__ = [
    "ActivityError",
    "ActivityResult",
    "AppendEventInput",
    "CompleteTaskInput",
    "EventRepository",
    "EventRepositoryError",
    "FailTaskInput",
    "RuntimeActivities",
    "ScheduleTaskInput",
    "StartRunInput",
    "StartRunResult",
]
