"""S2 runtime: Activity contracts — IO models and idempotency key builders。

事实源：specs/s2-agent-runtime.md §3/§4、S2-T3 plan。

Activities 是唯一副作用边界。IO 用 dataclass（temporalio 默认 converter 原生支持）；
幂等键由 workflow 按逻辑身份派生（run/task/attempt/transition），同一逻辑事件跨
activity 重试只落一次账（先查 has_event 再 append，冲突由 UoW fail-closed 拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_DEFAULT_ACTOR_REF = "agent-runtime:worker"


@dataclass
class StartRunActivityInput:
    """Input for the start_run activity."""

    run_id: str
    organization_id: str
    workspace_id: str
    graph: dict[str, Any]
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class ExecuteTaskInput:
    """Input for the execute_task activity.

    attempt_id 由 workflow 以 workflow.uuid4() 派生（replay 确定）；attempt_no 是
    workflow 侧的逻辑尝试序号，两者共同构成 TaskStarted 的幂等键。
    """

    run_id: str
    organization_id: str
    workspace_id: str
    task_id: str
    task_type: str
    handler_version: int
    attempt_id: str
    attempt_no: int
    input_values: dict[str, Any] = field(default_factory=dict)
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class RecordRunTerminalInput:
    """Input for recording a run-level terminal event."""

    run_id: str
    organization_id: str
    workspace_id: str
    outcome: str  # completed | failed | cancelled
    error: str | None = None
    reason: str | None = None
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class RecordTaskSkippedInput:
    """Input for recording a TaskSkipped event (unreachable after dep failure)."""

    run_id: str
    organization_id: str
    workspace_id: str
    task_id: str
    reason: str
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class CreateApprovalInput:
    """Input for the create_approval activity (RequestApproval tasks)."""

    run_id: str
    organization_id: str
    workspace_id: str
    task_id: str
    requested_by: str
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class RecordApprovalOutcomeInput:
    """Input for recording an approval task's terminal event after a decision."""

    run_id: str
    organization_id: str
    workspace_id: str
    task_id: str
    attempt_no: int
    decision: str  # approved | rejected
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class RecordTaskFailedInput:
    """Input for recording a TaskFailed event after activity retries exhausted."""

    run_id: str
    organization_id: str
    workspace_id: str
    task_id: str
    attempt_no: int
    error: str
    actor_ref: str = _DEFAULT_ACTOR_REF


@dataclass
class TaskExecutionResult:
    """Terminal outcome of one task attempt."""

    task_id: str
    status: str  # completed | failed
    attempt_no: int
    output_values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ActivityEventAck:
    """Acknowledgement that logical events are durably committed."""

    run_id: str
    created_events: int


def scheduled_key(run_id: str, task_id: str) -> str:
    return f"task:{run_id}:{task_id}:scheduled"


def started_key(run_id: str, task_id: str, attempt_no: int) -> str:
    return f"task:{run_id}:{task_id}:started:{attempt_no}"


def attempt_key(run_id: str, task_id: str, attempt_no: int) -> str:
    return f"task:{run_id}:{task_id}:attempt:{attempt_no}"


def terminal_key(run_id: str, task_id: str, attempt_no: int) -> str:
    return f"task:{run_id}:{task_id}:terminal:{attempt_no}"


def attempt_terminal_key(run_id: str, task_id: str, attempt_no: int) -> str:
    return f"task:{run_id}:{task_id}:attempt-terminal:{attempt_no}"


def run_created_key(run_id: str) -> str:
    return f"run:{run_id}:created"


def run_started_key(run_id: str) -> str:
    return f"run:{run_id}:started"


def run_terminal_key(run_id: str, outcome: str) -> str:
    return f"run:{run_id}:terminal:{outcome}"


def task_skipped_key(run_id: str, task_id: str) -> str:
    return f"task:{run_id}:{task_id}:skipped"


def approval_outcome_key(run_id: str, task_id: str, attempt_no: int) -> str:
    return f"task:{run_id}:{task_id}:approval:{attempt_no}"
