"""S2 runtime: pure reducer that replays events into Run projection。

事实源：design doc §4.3、S2-T2 plan、ADR-005（parallel merge semantics）。

The reducer is a pure function: same events always produce the same projection (determinism).
Event replay is idempotent — duplicate events are silently ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.agents.task_graph import MergeStrategy, TaskGraph
from zhiwei.runtime.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptCreated,
    ConflictDetected,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    RuntimeEvent,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)

# Terminal task statuses
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "skipped"})

# Terminal run statuses (run-level state machine)
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Allowed run-level state transitions
_VALID_RUN_TRANSITIONS: dict[str, set[str]] = {
    "created": {"running"},
    "running": {"completed", "failed", "cancelled", "paused"},
    "paused": {"running", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

# Allowed state transitions
# failed → started：业务失败后的 workflow 重试（新 attempt）重新打开 task；
# run 级终态守卫（run is terminal / RunCompleted 前置检查）独立承担终态不变量。
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "created": set(),
    "pending": {"scheduled", "skipped"},
    "scheduled": {"started", "completed", "failed", "skipped"},
    "started": {"completed", "failed"},
    "completed": set(),
    "failed": {"started"},
    "skipped": set(),
}


class ConflictRecord(BaseModel):
    """ADR-005: Records a conflict between parallel task outputs on the same field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    values: dict[str, Any]  # task_id -> value
    attempt_values: dict[str, UUID] = Field(default_factory=dict)  # task_id -> attempt_id
    evidence_refs: tuple[str, ...] = ()
    detected_at: datetime = Field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))


class AttemptState(BaseModel):
    """State of a single attempt within a task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    task_id: str
    attempt_number: int
    status: str = "pending"  # pending, committed, aborted


class TaskState(BaseModel):
    """Projection of a single task's state within a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    status: str = "pending"
    error: str | None = None
    current_attempt_id: UUID | None = None
    attempts: dict[UUID, AttemptState] = Field(default_factory=dict)
    output_values: dict[str, Any] = Field(default_factory=dict)


class RunState(BaseModel):
    """Projection of a run's state, built by replaying events through the reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: str = "created"
    graph: TaskGraph | None = None
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    canonical: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    # Internal: tracks which task_id wrote each canonical field (for LWW/conflict)
    field_owners: dict[str, str] = Field(default_factory=dict, exclude=True)

    @property
    def is_terminal(self) -> bool:
        """A run is terminal when its run-level status is terminal.

        Run-level terminal events (RunCompleted/RunFailed/RunCancelled) are emitted
        only after the workflow stops dispatching; task-level terminality alone
        (all tasks terminal) is necessary but not sufficient.
        """
        return self.status in TERMINAL_RUN_STATUSES

    def with_update(self, **kwargs: Any) -> RunState:
        """Return a new RunState with the given fields updated."""
        return self.model_copy(update=kwargs)


def _transition_task_status(current: str, new: str) -> str:
    """Validate and apply a task state transition."""
    allowed = _VALID_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise ValueError(f"Invalid task transition: {current} -> {new}")
    return new


def _transition_run_status(current: str, new: str) -> str:
    """Validate and apply a run-level state transition."""
    allowed = _VALID_RUN_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise ValueError(f"Invalid run transition: {current} -> {new}")
    return new


def _apply_merge_field(
    canonical: dict[str, Any],
    field: str,
    value: Any,
    task_id: str,
    strategy: MergeStrategy,
    field_owners: dict[str, str],
    timestamp: datetime | None = None,
    attempt_id: UUID | None = None,
) -> tuple[Any, ConflictRecord | None]:
    """Apply a merge strategy for a canonical field.

    Returns (merged_value, optional_conflict_record).
    field_owners tracks which task_id currently owns each field's value.
    """
    if timestamp is None:
        timestamp = datetime(1970, 1, 1, tzinfo=UTC)

    if strategy == MergeStrategy.APPEND:
        existing = canonical.get(field, [])
        if not isinstance(existing, list):
            existing = [existing]
        # Flatten: if value is a list, extend rather than nest
        if isinstance(value, list):
            return existing + value, None
        return [*existing, value], None

    if strategy == MergeStrategy.LAST_WRITE_WINS:
        prev = canonical.get(field)
        if prev is None:
            canonical[field] = value
            field_owners[field] = task_id
            return value, None
        # Task-id ordering: lower task_id wins
        prev_task = field_owners.get(field)
        if prev_task is None or task_id < prev_task:
            field_owners[field] = task_id
            return value, None
        return canonical.get(field), None

    if strategy == MergeStrategy.CONFLICT_PRESERVING:
        prev = canonical.get(field)
        if prev is None:
            canonical[field] = value
            field_owners[field] = task_id
            return value, None
        prev_task = field_owners.get(field)
        if prev_task is None:
            field_owners[field] = task_id
            return value, None
        # Both values coexist in a ConflictRecord
        values = {prev_task: prev, task_id: value}
        attempt_values: dict[str, UUID] = {}
        if attempt_id is not None:
            attempt_values[task_id] = attempt_id
        conflict = ConflictRecord(
            field=field,
            values=values,
            attempt_values=attempt_values,
            detected_at=timestamp,
        )
        return prev, conflict

    return value, None


def _handle_event(state: RunState, event: RuntimeEvent) -> RunState:
    """Apply a single event to the run state, returning a new RunState."""
    if isinstance(event, RunCreated):
        return RunState(
            run_id=event.run_id,
            status="created",
            graph=event.graph,
        )

    # run 终态后禁止新调度/新尝试；in-flight task 的结果事件（completed/failed/
    # skipped）仍允许落账——cancel 语义要求记录在途 effect 的最终归宿。
    if state.status in TERMINAL_RUN_STATUSES and isinstance(
        event, (TaskScheduled, TaskStarted, AttemptCreated)
    ):
        raise ValueError(
            f"run is terminal; cannot schedule or start task '{getattr(event, 'task_id', '')}'"
        )

    if isinstance(event, RunStarted):
        tasks = {}
        for task_id in state.graph.nodes if state.graph else []:
            tasks[task_id] = TaskState(task_id=task_id, status="pending")
        return state.with_update(status="running", tasks=tasks)

    if isinstance(event, RunCompleted):
        new_status = _transition_run_status(state.status, "completed")
        if not state.tasks or not all(
            task.status in TERMINAL_STATUSES for task in state.tasks.values()
        ):
            raise ValueError(
                "run completion requires all tasks terminal before RunCompleted"
            )
        return state.with_update(status=new_status)

    if isinstance(event, RunFailed):
        new_status = _transition_run_status(state.status, "failed")
        return state.with_update(status=new_status)

    if isinstance(event, RunCancelled):
        new_status = _transition_run_status(state.status, "cancelled")
        # cancel 停止新 task：从未启动的 task 记录为 skipped（在途状态留痕），
        # 已启动的 task 允许其 in-flight 结果事件随后落账（见 task 事件守卫）。
        tasks = {}
        for task_id, task in state.tasks.items():
            if task.status in {"pending", "scheduled"}:
                tasks[task_id] = task.model_copy(update={"status": "skipped"})
            else:
                tasks[task_id] = task
        return state.with_update(status=new_status, tasks=tasks)

    if isinstance(event, RunPaused):
        new_status = _transition_run_status(state.status, "paused")
        return state.with_update(status=new_status)

    if isinstance(event, RunResumed):
        new_status = _transition_run_status(state.status, "running")
        return state.with_update(status=new_status)

    if isinstance(event, TaskScheduled):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        new_status = _transition_task_status(task.status, "scheduled")
        tasks[event.task_id] = task.model_copy(update={"status": new_status})
        return state.with_update(tasks=tasks)

    if isinstance(event, TaskStarted):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        new_status = _transition_task_status(task.status, "started")
        attempts = dict(task.attempts)
        # AttemptCreated 先行携带权威序号；TaskStarted 只补登 attempt（重试/直接
        # start 两种事件序下序号都不得被 len(attempts)+1 覆盖——生产事件序恒为
        # TaskScheduled → AttemptCreated(n) → TaskStarted）
        existing = attempts.get(event.attempt_id)
        if existing is None:
            existing = AttemptState(
                id=event.attempt_id,
                task_id=event.task_id,
                attempt_number=len(attempts) + 1,
            )
        attempts[event.attempt_id] = existing
        tasks[event.task_id] = task.model_copy(update={
            "status": new_status,
            "current_attempt_id": event.attempt_id,
            "attempts": attempts,
        })
        return state.with_update(tasks=tasks)

    if isinstance(event, TaskCompleted):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        new_status = _transition_task_status(task.status, "completed")
        tasks[event.task_id] = task.model_copy(update={
            "status": new_status,
            "output_values": event.output_values,
        })

        # Apply merge strategies for output fields
        canonical = dict(state.canonical)
        conflicts = list(state.conflicts)
        field_owners = dict(state.field_owners)
        current_attempt_id = task.current_attempt_id
        if state.graph and event.task_id in state.graph.nodes:
            node = state.graph.nodes[event.task_id]
            for field, value in event.output_values.items():
                strategy = node.output_merge_strategies.get(field)
                if strategy is None:
                    # No merge strategy declared — simple overwrite for non-parallel fields
                    canonical[field] = value
                    field_owners[field] = event.task_id
                else:
                    merged, conflict = _apply_merge_field(
                        canonical, field, value, event.task_id,
                        strategy, field_owners,
                        timestamp=event.timestamp,
                        attempt_id=current_attempt_id,
                    )
                    canonical[field] = merged
                    if conflict is not None:
                        conflicts.append(conflict)

        result = state.with_update(
            tasks=tasks, canonical=canonical, conflicts=conflicts,
            field_owners=field_owners,
        )
        return result

    if isinstance(event, TaskFailed):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        new_status = _transition_task_status(task.status, "failed")
        tasks[event.task_id] = task.model_copy(update={"status": new_status, "error": event.error})
        return state.with_update(tasks=tasks)

    if isinstance(event, TaskSkipped):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        new_status = _transition_task_status(task.status, "skipped")
        tasks[event.task_id] = task.model_copy(update={"status": new_status})
        return state.with_update(tasks=tasks)

    if isinstance(event, AttemptCreated):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        attempts = dict(task.attempts)
        attempts[event.attempt_id] = AttemptState(
            id=event.attempt_id,
            task_id=event.task_id,
            attempt_number=event.attempt_number,
        )
        tasks[event.task_id] = task.model_copy(update={
            "attempts": attempts,
            "current_attempt_id": event.attempt_id,
        })
        return state.with_update(tasks=tasks)

    if isinstance(event, AttemptCommitted):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        attempts = dict(task.attempts)
        attempt = attempts.get(event.attempt_id)
        if attempt is None:
            return state
        attempts[event.attempt_id] = attempt.model_copy(update={"status": "committed"})
        tasks[event.task_id] = task.model_copy(update={"attempts": attempts})
        return state.with_update(tasks=tasks)

    if isinstance(event, AttemptAborted):
        tasks = dict(state.tasks)
        task = tasks.get(event.task_id)
        if task is None:
            return state
        attempts = dict(task.attempts)
        attempt = attempts.get(event.attempt_id)
        if attempt is None:
            return state
        attempts[event.attempt_id] = attempt.model_copy(update={"status": "aborted"})
        tasks[event.task_id] = task.model_copy(update={"attempts": attempts})
        return state.with_update(tasks=tasks)

    if isinstance(event, ConflictDetected):
        conflicts = list(state.conflicts)
        evidence_refs_raw = event.conflict_record.get("evidence_refs", [])
        evidence_refs = tuple(evidence_refs_raw) if isinstance(evidence_refs_raw, list) else ()
        conflicts.append(
            ConflictRecord(
                field=event.field,
                values=event.conflict_record.get("values", {}),
                evidence_refs=evidence_refs,
                detected_at=event.timestamp,
            )
        )
        return state.with_update(conflicts=conflicts)

    return state


def reduce(events: list[RuntimeEvent]) -> RunState:
    """Pure reducer: replay events into a Run projection.

    Same events always produce the same projection (determinism).
    Duplicate events are silently ignored (idempotency).
    """
    seen_event_ids: set[UUID] = set()
    state = RunState(run_id=UUID(int=0))  # Placeholder; overwritten by RunCreated

    for event in events:
        if event.event_id in seen_event_ids:
            continue
        seen_event_ids.add(event.event_id)
        state = _handle_event(state, event)

    return state
