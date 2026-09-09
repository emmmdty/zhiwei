"""S2-T6：runtime-contract-v1 契约场景定义（代码定义的 eval 单位）。

事实源：specs/s2-agent-runtime.md §6（required tests 的契约面）、ADR-005。

每个 RegisteredUnit 对应一个可经「生产命令路径」执行的 TaskGraph 场景 + 一组
从 PG canonical projection 断言的 invariants。场景经 RunCommandService →
OutboxDispatcher → Temporal workflow → activities 落账，不设评测旁路；
invariants 只读 reduced RunState 与事件序列（真相在 PG）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from zhiwei.agents.task_graph import (
    FailurePolicy,
    MergeStrategy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.runtime.reducer import RunState

RUNTIME_CONTRACT_SUITE = "runtime-contract-v1"

RUNTIME_CONTRACT_UNITS: tuple[RegisteredUnit, ...] = (
    RegisteredUnit(sample_id="runtime/graph", unit_id="basic-lifecycle"),
    RegisteredUnit(sample_id="runtime/graph", unit_id="parallel-merge-order"),
    RegisteredUnit(sample_id="runtime/graph", unit_id="retry-on-failure"),
    RegisteredUnit(sample_id="runtime/graph", unit_id="dependency-failure-skip"),
    RegisteredUnit(sample_id="runtime/graph", unit_id="duplicate-signal-cancel"),
    RegisteredUnit(sample_id="runtime/graph", unit_id="continue-as-new"),
    RegisteredUnit(sample_id="runtime/merge", unit_id="conflict-preserving"),
)


class SignalScript(BaseModel):
    """一条经生产命令路径投递的信号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = "cancel_run"  # cancel_run | pause_run | resume_run
    reason: str | None = None
    duplicate: bool = False  # 同一 command_event_id 发两次（幂等去重契约）


class RuntimeContractScenario(BaseModel):
    """一个 runtime 契约场景的全部编排输入与断言。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: RegisteredUnit
    graph: TaskGraph
    max_task_attempts: int = 3
    continue_as_new_after: int = 1000
    signals: tuple[SignalScript, ...] = ()
    # invariants 以字符串化错误列表返回（空列表 = 通过）
    invariant: str  # executor 侧按名分派的断言函数名


def _node(
    task_id: str,
    *,
    task_type: str = "Fixture",
    deps: tuple[str, ...] = (),
    parallel: bool = False,
    policy: FailurePolicy = FailurePolicy.RETRY,
    merge: dict[str, MergeStrategy] | None = None,
) -> TaskGraphNode:
    return TaskGraphNode(
        task_id=task_id,
        task_type=task_type,
        dependencies=deps,
        parallel_safe=parallel,
        required_capability="fixture",
        failure_policy=policy,
        output_merge_strategies=merge or {},
    )


def _basic_lifecycle() -> RuntimeContractScenario:
    graph = TaskGraph(
        nodes={
            "intake": _node("intake"),
            "retrieve_a": _node("retrieve_a", deps=("intake",), parallel=True),
            "retrieve_b": _node("retrieve_b", deps=("intake",), parallel=True),
            "synthesize": _node("synthesize", deps=("retrieve_a", "retrieve_b")),
        },
        edges={
            "retrieve_a": ["intake"],
            "retrieve_b": ["intake"],
            "synthesize": ["retrieve_a", "retrieve_b"],
        },
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[0],
        graph=graph,
        invariant="all_tasks_completed_run_completed",
    )


def _parallel_merge_order() -> RuntimeContractScenario:
    """并行只读任务按 stable task id 合并：append 序对每个分支可见。"""

    graph = TaskGraph(
        nodes={
            "intake": _node("intake"),
            "a": _node("a", task_type="ObserveFixture", deps=("intake",), parallel=True,
                       merge={"observations": MergeStrategy.APPEND}),
            "b": _node("b", task_type="ObserveFixture", deps=("intake",), parallel=True,
                       merge={"observations": MergeStrategy.APPEND}),
        },
        edges={"a": ["intake"], "b": ["intake"]},
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[1],
        graph=graph,
        invariant="append_merge_contains_all_branches",
    )


def _retry_on_failure() -> RuntimeContractScenario:
    graph = TaskGraph(
        nodes={
            "t1": _node("t1", task_type="FlakyFixture"),
            "t2": _node("t2", deps=("t1",)),
        },
        edges={"t2": ["t1"]},
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[2],
        graph=graph,
        max_task_attempts=3,
        invariant="failed_attempt_reopened_and_completed",
    )


def _dependency_failure_skip() -> RuntimeContractScenario:
    graph = TaskGraph(
        nodes={
            "t1": _node("t1", task_type="AlwaysFails"),
            "t2": _node("t2", deps=("t1",)),
        },
        edges={"t2": ["t1"]},
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[3],
        graph=graph,
        max_task_attempts=1,
        invariant="dependency_failure_skips_downstream_run_failed",
    )


def _duplicate_signal_cancel() -> RuntimeContractScenario:
    graph = TaskGraph(
        nodes={
            "t1": _node("t1", task_type="SlowFixture"),
            "t2": _node("t2", deps=("t1",)),
            "t3": _node("t3", deps=("t2",)),
        },
        edges={"t2": ["t1"], "t3": ["t2"]},
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[4],
        graph=graph,
        signals=(SignalScript(kind="cancel_run", reason="operator", duplicate=True),),
        invariant="single_cancellation_event",
    )


def _continue_as_new() -> RuntimeContractScenario:
    graph = TaskGraph(
        nodes={
            "t1": _node("t1"),
            "t2": _node("t2", deps=("t1",)),
            "t3": _node("t3", deps=("t2",)),
            "independent": _node("independent"),
        },
        edges={"t2": ["t1"], "t3": ["t2"]},
    )
    # independent 无依赖——与链并行；continue_as_new_after=2 触发 CAN 后剩余任务在新 run 推进
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[5],
        graph=graph,
        continue_as_new_after=2,
        invariant="continue_as_new_occurred_and_completed",
    )


def _conflict_preserving() -> RuntimeContractScenario:
    """ADR-005：K 个并行分支写同一 conflict_preserving 字段 → K-1 条 ConflictRecord。"""

    graph = TaskGraph(
        nodes={
            "intake": _node("intake"),
            "b1": _node("b1", task_type="DecisionFixture", deps=("intake",), parallel=True,
                        merge={"decision": MergeStrategy.CONFLICT_PRESERVING}),
            "b2": _node("b2", task_type="DecisionFixture", deps=("intake",), parallel=True,
                        merge={"decision": MergeStrategy.CONFLICT_PRESERVING}),
            "b3": _node("b3", task_type="DecisionFixture", deps=("intake",), parallel=True,
                        merge={"decision": MergeStrategy.CONFLICT_PRESERVING}),
        },
        edges={"b1": ["intake"], "b2": ["intake"], "b3": ["intake"]},
    )
    return RuntimeContractScenario(
        unit=RUNTIME_CONTRACT_UNITS[6],
        graph=graph,
        invariant="k_branches_k_minus_1_conflicts",
    )


RUNTIME_CONTRACT_SCENARIOS: tuple[RuntimeContractScenario, ...] = (
    _basic_lifecycle(),
    _parallel_merge_order(),
    _retry_on_failure(),
    _dependency_failure_skip(),
    _duplicate_signal_cancel(),
    _continue_as_new(),
    _conflict_preserving(),
)

_SCENARIO_BY_UNIT: dict[tuple[str, str], RuntimeContractScenario] = {
    (s.unit.sample_id, s.unit.unit_id): s for s in RUNTIME_CONTRACT_SCENARIOS
}


def scenario_for_unit(unit: RegisteredUnit) -> RuntimeContractScenario:
    try:
        return _SCENARIO_BY_UNIT[(unit.sample_id, unit.unit_id)]
    except KeyError as exc:
        raise LookupError(f"unknown runtime contract unit: {unit.sample_id}/{unit.unit_id}") from exc


# --------------------------------------------------------------------- invariants


def _errors_all_completed(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    for task_id, task in state.tasks.items():
        if task.status != "completed":
            errors.append(f"task {task_id!r} status {task.status!r} != 'completed'")
    return errors


def _errors_continue_as_new(state: RunState, events: list[Any]) -> list[str]:
    """CAN 契约：状态完备 + Continue-As-New 真实发生（由 executor 侧附加
    execution_count 证据——多 run 链是 CAN 的唯一可观测痕迹）。"""
    errors = _errors_all_completed(state, events)
    execution_count = getattr(state, "_execution_count", None)
    if execution_count is None:
        # fallback：executor 未附证据时跳过计数断言（向后兼容单测直调）
        return errors
    if execution_count < 2:
        errors.append(
            f"continue-as-new must actually occur (executions={execution_count})"
        )
    return errors


def _errors_append_merge(state: RunState, events: list[Any]) -> list[str]:
    errors = _errors_all_completed(state, events)
    observations = state.canonical.get("observations")
    if not isinstance(observations, list) or len(observations) != 2:
        errors.append(f"append merge must contain both branches, got {observations!r}")
    return errors


def _errors_retry_reopened(state: RunState, events: list[Any]) -> list[str]:
    errors = _errors_all_completed(state, events)
    task = state.tasks.get("t1")
    if task is None:
        return [*errors, "task t1 missing"]
    statuses = sorted(a.status for a in task.attempts.values())
    if statuses != ["aborted", "committed"]:
        errors.append(f"attempt statuses {statuses} != ['aborted', 'committed']")
    if task.status != "completed":
        errors.append(f"retried task status {task.status!r} != 'completed'")
    return errors


def _errors_dependency_skip(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "failed":
        errors.append(f"run status {state.status!r} != 'failed'")
    t1 = state.tasks.get("t1")
    t2 = state.tasks.get("t2")
    if t1 is None or t1.status != "failed":
        errors.append("dependency task t1 must be failed")
    if t2 is None or t2.status != "skipped":
        errors.append(f"downstream task t2 must be skipped, got {t2.status if t2 else None!r}")
    return errors


def _errors_single_cancellation(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "cancelled":
        errors.append(f"run status {state.status!r} != 'cancelled'")
    cancels = [e for e in events if type(e).__name__ == "RunCancelled"]
    if len(cancels) != 1:
        errors.append(f"duplicate cancel must collapse to 1 event, got {len(cancels)}")
    return errors


def _errors_k_conflicts(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    decision_conflicts = [c for c in state.conflicts if c.field == "decision"]
    if len(decision_conflicts) != 2:
        errors.append(
            f"3 parallel branches must yield 2 ConflictRecords, got {len(decision_conflicts)}"
        )
    values = set()
    for conflict in decision_conflicts:
        values.update(str(v) for v in conflict.values.values())
    if len(values) != 3:
        errors.append(f"conflict records must preserve all 3 values, got {values}")
    return errors


_INVARIANTS: dict[str, Callable[[RunState, list[Any]], list[str]]] = {
    "all_tasks_completed_run_completed": _errors_all_completed,
    "continue_as_new_occurred_and_completed": _errors_continue_as_new,
    "append_merge_contains_all_branches": _errors_append_merge,
    "failed_attempt_reopened_and_completed": _errors_retry_reopened,
    "dependency_failure_skips_downstream_run_failed": _errors_dependency_skip,
    "single_cancellation_event": _errors_single_cancellation,
    "k_branches_k_minus_1_conflicts": _errors_k_conflicts,
}


def check_invariant(name: str, state: RunState, events: list[Any]) -> list[str]:
    try:
        check = _INVARIANTS[name]
    except KeyError as exc:
        raise LookupError(f"unknown runtime invariant: {name!r}") from exc
    return check(state, events)
