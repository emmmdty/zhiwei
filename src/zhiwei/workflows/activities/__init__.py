"""S2 runtime: Activity contracts and real Temporal activity implementations。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan。

Activities are the side-effect boundary — they append PG events idempotently.
Workflow orchestration calls activities; activities never call back into workflow.
"""

from __future__ import annotations

from zhiwei.workflows.activities.base import (
    ActivityEventAck,
    ExecuteTaskInput,
    RecordRunTerminalInput,
    RecordTaskFailedInput,
    RecordTaskSkippedInput,
    StartRunActivityInput,
    TaskExecutionResult,
)
from zhiwei.workflows.activities.runtime import RuntimeActivities

__all__ = [
    "ActivityEventAck",
    "ExecuteTaskInput",
    "RecordRunTerminalInput",
    "RecordTaskFailedInput",
    "RecordTaskSkippedInput",
    "RuntimeActivities",
    "StartRunActivityInput",
    "TaskExecutionResult",
]
