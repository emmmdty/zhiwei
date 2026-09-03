"""S2 runtime: Agent Runtime modules。

事实源：design doc §4、S2-T2 plan。

Event-driven runtime with pure reducer, task scheduler, and attempt lifecycle management.
"""

from __future__ import annotations

from zhiwei.runtime.attempts import AttemptError, AttemptManager, AttemptRecord
from zhiwei.runtime.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptCreated,
    ConflictDetected,
    RunCreated,
    RunStarted,
    RuntimeEvent,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.reducer import (
    ConflictRecord,
    RunState,
    TaskState,
    reduce,
)
from zhiwei.runtime.scheduler import Scheduler, SchedulerError

__all__ = [
    "AttemptAborted",
    "AttemptCommitted",
    "AttemptCreated",
    "AttemptError",
    "AttemptManager",
    "AttemptRecord",
    "ConflictDetected",
    "ConflictRecord",
    "RunCreated",
    "RunStarted",
    "RunState",
    "RuntimeEvent",
    "Scheduler",
    "SchedulerError",
    "TaskCompleted",
    "TaskFailed",
    "TaskScheduled",
    "TaskSkipped",
    "TaskStarted",
    "TaskState",
    "reduce",
]
