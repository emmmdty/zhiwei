"""S2-T2 RED: Pure reducer tests — event replay, merge strategies, terminal invariant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zhiwei.agents.task_graph import (
    FailurePolicy,
    MergeStrategy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.events import (
    AttemptAborted,
    AttemptCommitted,
    AttemptCreated,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    TaskCompleted,
    TaskFailed,
    TaskScheduled,
    TaskSkipped,
    TaskStarted,
)
from zhiwei.runtime.reducer import (
    RunState,
    TaskState,
    _transition_task_status,
    reduce,
)


def _ts(offset_seconds: int = 0) -> datetime:
    return datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds)


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
                output_merge_strategies={"entity_binding": MergeStrategy.CONFLICT_PRESERVING},
            ),
            "retrieve_a": TaskGraphNode(
                task_id="retrieve_a",
                task_type="Retrieve",
                dependencies=("intake",),
                parallel_safe=True,
                required_capability="retrieve",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
                output_merge_strategies={"entity_binding": MergeStrategy.CONFLICT_PRESERVING},
            ),
            "retrieve_b": TaskGraphNode(
                task_id="retrieve_b",
                task_type="Retrieve",
                dependencies=("intake",),
                parallel_safe=True,
                required_capability="retrieve",
                budget={},
                failure_policy=FailurePolicy.RETRY,
                completion_obligations=(),
                output_merge_strategies={"entity_binding": MergeStrategy.CONFLICT_PRESERVING},
            ),
            "synthesize": TaskGraphNode(
                task_id="synthesize",
                task_type="Synthesize",
                dependencies=("retrieve_a", "retrieve_b"),
                parallel_safe=False,
                required_capability="synthesize",
                budget={},
                failure_policy=FailurePolicy.CANCEL,
                completion_obligations=(),
                output_merge_strategies={},
            ),
        },
        edges={
            "intake": [],
            "retrieve_a": ["intake"],
            "retrieve_b": ["intake"],
            "synthesize": ["retrieve_a", "retrieve_b"],
        },
    )


class TestRunState:
    def test_initial_state_has_no_tasks(self) -> None:
        state = RunState(run_id=new_id())
        assert state.status == "created"
        assert state.tasks == {}
        assert state.conflicts == []

    def test_initial_state_is_not_terminal(self) -> None:
        state = RunState(run_id=new_id())
        assert not state.is_terminal

    def test_terminal_state_when_all_tasks_terminal(self) -> None:
        state = RunState(run_id=new_id())
        state = state.with_update(tasks={
            "t1": TaskState(task_id="t1", status="completed"),
            "t2": TaskState(task_id="t2", status="failed"),
        })
        # 收紧后的契约：task 全终态但 run 级终态事件缺失时，run 仍不是 terminal
        assert not state.is_terminal
        state = state.with_update(status="completed")
        assert state.is_terminal


class TestReducerDeterminism:
    def test_same_events_always_produce_same_projection(self) -> None:
        run_id = new_id()
        events1 = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=new_id()),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
        ]
        events2 = list(events1)

        state1 = reduce(events1)
        state2 = reduce(events2)
        assert state1.tasks.keys() == state2.tasks.keys()
        for key in state1.tasks:
            assert state1.tasks[key].status == state2.tasks[key].status

    def test_duplicate_event_idempotency(self) -> None:
        run_id = new_id()
        event = TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake")
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            event,
            event,
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "scheduled"


class TestReducerRunLifecycle:
    def test_run_created_sets_initial_status(self) -> None:
        run_id = new_id()
        events: list[object] = [RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph())]
        state = reduce(events)  # type: ignore[arg-type]
        assert state.status == "created"
        assert state.run_id == run_id

    def test_run_started_transitions_to_running(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        state = reduce(events)
        assert state.status == "running"

    def test_task_lifecycle_scheduled_to_completed(self) -> None:
        run_id = new_id()
        attempt_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=attempt_id),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="intake", output_values={}),
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "completed"

    def test_task_failure_records_failure_info(self) -> None:
        run_id = new_id()
        attempt_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=attempt_id),
            TaskFailed(
                run_id=run_id, timestamp=_ts(4), task_id="intake",
                error="boom", attempt_id=attempt_id,
            ),
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "failed"
        assert state.tasks["intake"].error == "boom"

    def test_task_skipped_marks_terminal(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskSkipped(run_id=run_id, timestamp=_ts(3), task_id="intake", reason="not needed"),
        ]
        state = reduce(events)
        assert state.tasks["intake"].status == "skipped"


class TestAttemptLifecycle:
    def test_attempt_created_records_attempt(self) -> None:
        run_id = new_id()
        attempt_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            AttemptCreated(
                run_id=run_id, timestamp=_ts(3), task_id="intake",
                attempt_id=attempt_id, attempt_number=1,
            ),
        ]
        state = reduce(events)
        assert state.tasks["intake"].current_attempt_id == attempt_id

    def test_attempt_committed_transitions_attempt(self) -> None:
        run_id = new_id()
        attempt_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            AttemptCreated(
                run_id=run_id, timestamp=_ts(3), task_id="intake",
                attempt_id=attempt_id, attempt_number=1,
            ),
            AttemptCommitted(run_id=run_id, timestamp=_ts(4), task_id="intake", attempt_id=attempt_id),
        ]
        state = reduce(events)
        attempt = state.tasks["intake"].attempts[attempt_id]
        assert attempt.status == "committed"

    def test_attempt_aborted_transitions_attempt(self) -> None:
        run_id = new_id()
        attempt_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            AttemptCreated(
                run_id=run_id, timestamp=_ts(3), task_id="intake",
                attempt_id=attempt_id, attempt_number=1,
            ),
            AttemptAborted(run_id=run_id, timestamp=_ts(4), task_id="intake", attempt_id=attempt_id),
        ]
        state = reduce(events)
        attempt = state.tasks["intake"].attempts[attempt_id]
        assert attempt.status == "aborted"


class TestTerminalInvariant:
    def test_run_terminal_when_all_tasks_terminal(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve_a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve_b"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="synthesize"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="retrieve_a", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(5), task_id="retrieve_b", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(6), task_id="synthesize", output_values={}),
            RunCompleted(run_id=run_id, timestamp=_ts(7)),
        ]
        state = reduce(events)
        assert state.is_terminal

    def test_run_not_terminal_when_tasks_pending(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve_a"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
        ]
        state = reduce(events)
        assert not state.is_terminal

    def test_run_terminal_with_mixed_terminal_statuses(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve_a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="retrieve_b"),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="synthesize"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskCompleted(run_id=run_id, timestamp=_ts(4), task_id="retrieve_a", output_values={}),
            TaskFailed(run_id=run_id, timestamp=_ts(5), task_id="retrieve_b",
                       error="timeout", attempt_id=new_id()),
            TaskSkipped(run_id=run_id, timestamp=_ts(6), task_id="synthesize", reason="deps failed"),
            RunFailed(run_id=run_id, timestamp=_ts(7), error="retrieve_b failed"),
        ]
        state = reduce(events)
        assert state.is_terminal


class TestParallelMergeConflicts:
    def test_conflict_preserving_generates_conflict_record(self) -> None:
        run_id = new_id()
        a_id = new_id()
        b_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="retrieve_a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="retrieve_b"),
            TaskStarted(run_id=run_id, timestamp=_ts(5), task_id="retrieve_a", attempt_id=a_id),
            TaskStarted(run_id=run_id, timestamp=_ts(5), task_id="retrieve_b", attempt_id=b_id),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(6), task_id="retrieve_a",
                output_values={"entity_binding": {"entity": "Alice", "role": "author"}},
            ),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(7), task_id="retrieve_b",
                output_values={"entity_binding": {"entity": "Bob", "role": "reviewer"}},
            ),
        ]
        state = reduce(events)
        assert len(state.conflicts) == 1
        conflict = state.conflicts[0]
        assert conflict.field == "entity_binding"
        assert set(conflict.values.keys()) == {"retrieve_a", "retrieve_b"}

    def test_append_merge_preserves_order(self) -> None:
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"evidence_refs": MergeStrategy.APPEND},
                ),
                "r_a": TaskGraphNode(
                    task_id="r_a", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"evidence_refs": MergeStrategy.APPEND},
                ),
                "r_b": TaskGraphNode(
                    task_id="r_b", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"evidence_refs": MergeStrategy.APPEND},
                ),
            },
            edges={"intake": [], "r_a": ["intake"], "r_b": ["intake"]},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_b"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(5), task_id="r_a",
                output_values={"evidence_refs": ["ref_1"]},
            ),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(6), task_id="r_b",
                output_values={"evidence_refs": ["ref_2"]},
            ),
        ]
        state = reduce(events)
        evidence = state.canonical.get("evidence_refs")
        assert isinstance(evidence, list)
        assert "ref_1" in evidence
        assert "ref_2" in evidence
        idx_a = evidence.index("ref_1")
        idx_b = evidence.index("ref_2")
        assert idx_a < idx_b

    def test_last_write_wins_uses_task_id_order(self) -> None:
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"progress": MergeStrategy.LAST_WRITE_WINS},
                ),
                "a": TaskGraphNode(
                    task_id="a", task_type="Analyze", dependencies=("intake",),
                    parallel_safe=True, required_capability="analyze", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"progress": MergeStrategy.LAST_WRITE_WINS},
                ),
                "b": TaskGraphNode(
                    task_id="b", task_type="Analyze", dependencies=("intake",),
                    parallel_safe=True, required_capability="analyze", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"progress": MergeStrategy.LAST_WRITE_WINS},
                ),
            },
            edges={"intake": [], "a": ["intake"], "b": ["intake"]},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="b"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(5), task_id="a",
                output_values={"progress": "from_a"},
            ),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(6), task_id="b",
                output_values={"progress": "from_b"},
            ),
        ]
        state = reduce(events)
        # "a" sorts before "b", so "a" wins under task-id ordering
        assert state.canonical["progress"] == "from_a"

    def test_parallel_merge_k_branches_k_minus_1_conflicts(self) -> None:
        run_id = new_id()
        task_ids = ["r_a", "r_b", "r_c"]
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"decision": MergeStrategy.CONFLICT_PRESERVING},
                ),
                **{
                    tid: TaskGraphNode(
                        task_id=tid, task_type="Retrieve", dependencies=("intake",),
                        parallel_safe=True, required_capability="retrieve", budget={},
                        failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                        output_merge_strategies={"decision": MergeStrategy.CONFLICT_PRESERVING},
                    )
                    for tid in task_ids
                },
            },
            edges={"intake": [], **{tid: ["intake"] for tid in task_ids}},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
        ]
        for tid in task_ids:
            events.append(TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id=tid))
        for i, tid in enumerate(task_ids):
            events.append(TaskCompleted(
                run_id=run_id, timestamp=_ts(5 + i), task_id=tid,
                output_values={"decision": f"value_{tid}"},
            ))
        state = reduce(events)
        decision_conflicts = [c for c in state.conflicts if c.field == "decision"]
        assert len(decision_conflicts) == 2  # K-1 = 3-1 = 2


class TestInvalidStateTransitions:
    def test_pending_to_completed_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task transition"):
            _transition_task_status("pending", "completed")

    def test_pending_to_started_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task transition"):
            _transition_task_status("pending", "started")

    def test_scheduled_to_pending_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid task transition"):
            _transition_task_status("scheduled", "pending")

    def test_completed_to_any_raises(self) -> None:
        for target in ("pending", "scheduled", "started", "failed", "skipped"):
            with pytest.raises(ValueError, match="Invalid task transition"):
                _transition_task_status("completed", target)

    def test_failed_to_any_raises(self) -> None:
        # 修订：failed → started 是合法的 workflow 重试转移（新 attempt 重新打开 task）
        for target in ("pending", "scheduled", "completed", "skipped"):
            with pytest.raises(ValueError, match="Invalid task transition"):
                _transition_task_status("failed", target)

    def test_skipped_to_any_raises(self) -> None:
        for target in ("pending", "scheduled", "started", "completed", "failed"):
            with pytest.raises(ValueError, match="Invalid task transition"):
                _transition_task_status("skipped", target)


class TestRunLevelTerminalEvents:
    """S2 收口 RED：run 级终态/暂停事件——workflow 的 cancel/complete 语义需要
    canonical 事件承载（spec §4「cancel 停止新 task，并记录在途 effect state」）。"""

    def _terminal_events(self, run_id) -> list:
        graph = _make_graph()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        for i, task_id in enumerate(sorted(graph.nodes)):
            events.append(TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id=task_id))
            events.append(
                TaskCompleted(run_id=run_id, timestamp=_ts(3 + i), task_id=task_id, output_values={})
            )
        return events

    def test_run_completed_transitions_status(self) -> None:
        run_id = new_id()
        events = [*self._terminal_events(run_id),
            RunCompleted(run_id=run_id, timestamp=_ts(4)),
        ]
        state = reduce(events)
        assert state.status == "completed"
        assert state.is_terminal

    def test_run_completed_before_tasks_terminal_rejected(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            RunCompleted(run_id=run_id, timestamp=_ts(2)),
        ]
        with pytest.raises(ValueError, match="run completion requires all tasks terminal"):
            reduce(events)

    def test_run_failed_after_task_failure(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskFailed(run_id=run_id, timestamp=_ts(3), task_id="intake", error="boom"),
            RunFailed(run_id=run_id, timestamp=_ts(4), error="boom"),
        ]
        state = reduce(events)
        assert state.status == "failed"

    def test_run_cancelled_stops_pending_tasks_as_skipped(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            RunCancelled(run_id=run_id, timestamp=_ts(4), reason="operator request"),
        ]
        state = reduce(events)
        assert state.status == "cancelled"
        # cancel 停止新 task：未终态 task 被标记 skipped（记录在途状态）
        assert all(t.status in {"completed", "failed", "skipped"} for t in state.tasks.values())

    def test_run_cancelled_from_running_only(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunCancelled(run_id=run_id, timestamp=_ts(1), reason="too early"),
        ]
        with pytest.raises(ValueError, match="Invalid run transition"):
            reduce(events)

    def test_run_cancelled_twice_rejected(self) -> None:
        run_id = new_id()
        events = [*self._terminal_events(run_id),
            RunCancelled(run_id=run_id, timestamp=_ts(4), reason="a"),
            RunCancelled(run_id=run_id, timestamp=_ts(5), reason="again"),
        ]
        with pytest.raises(ValueError, match="Invalid run transition"):
            reduce(events)

    def test_pause_resume_round_trip(self) -> None:
        run_id = new_id()
        graph = _make_graph()
        events: list = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            RunPaused(run_id=run_id, timestamp=_ts(2), reason="backpressure"),
            RunResumed(run_id=run_id, timestamp=_ts(3)),
        ]
        for i, task_id in enumerate(sorted(graph.nodes)):
            events.append(TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id=task_id))
            events.append(
                TaskCompleted(run_id=run_id, timestamp=_ts(5 + i), task_id=task_id, output_values={})
            )
        events.append(RunCompleted(run_id=run_id, timestamp=_ts(9)))
        state = reduce(events)
        assert state.status == "completed"

    def test_resume_without_pause_rejected(self) -> None:
        run_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            RunResumed(run_id=run_id, timestamp=_ts(2)),
        ]
        with pytest.raises(ValueError, match="Invalid run transition"):
            reduce(events)

    def test_terminal_run_rejects_further_task_events(self) -> None:
        run_id = new_id()
        events = [*self._terminal_events(run_id),
            RunCompleted(run_id=run_id, timestamp=_ts(4)),
            TaskScheduled(run_id=run_id, timestamp=_ts(5), task_id="retrieve_a"),
        ]
        with pytest.raises(ValueError, match="run is terminal"):
            reduce(events)


class TestTaskRetryReopensFailedTask:
    """S2 收口 RED：业务失败后的 workflow 重试（新 attempt）必须能重新打开 failed task。

    真实 Temporal 集成（TestActivityRetry）暴露：attempt 1 TaskFailed 把 task 投影为
    failed，attempt 2 的 TaskStarted 被「failed → started 非法」拒绝。任务级终态的
    语义是「当前无在途 attempt」，重试合法；run 级终态守卫仍由 RunCompleted 前置
    检查承担。
    """

    def test_failed_task_restarts_with_new_attempt(self) -> None:
        run_id = new_id()
        attempt_1, attempt_2 = new_id(), new_id()
        graph = _make_graph()
        events: list = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskStarted(run_id=run_id, timestamp=_ts(3), task_id="intake", attempt_id=attempt_1),
            TaskFailed(run_id=run_id, timestamp=_ts(4), task_id="intake", error="flaky", attempt_id=attempt_1),
            TaskStarted(run_id=run_id, timestamp=_ts(5), task_id="intake", attempt_id=attempt_2),
            TaskCompleted(run_id=run_id, timestamp=_ts(6), task_id="intake", output_values={}),
        ]
        for tid in sorted(graph.nodes):
            if tid == "intake":
                continue
            events.append(TaskScheduled(run_id=run_id, timestamp=_ts(7), task_id=tid))
            events.append(TaskCompleted(run_id=run_id, timestamp=_ts(8), task_id=tid, output_values={}))
        events.append(RunCompleted(run_id=run_id, timestamp=_ts(9)))
        state = reduce(events)
        assert state.tasks["intake"].status == "completed"
        assert len(state.tasks["intake"].attempts) == 2
        assert state.status == "completed"


class TestAttemptNumberPreservation:
    """S2 修复轮 RED（Reviewer B #3）：TaskStarted 不得覆盖 AttemptCreated 的序号。

    生产事件序恒为 TaskScheduled → AttemptCreated(n) → TaskStarted（同 attempt），
    旧实现无条件以 len(attempts)+1 重算序号，每个 attempt 的投影序号恒 +1。
    """

    def test_task_started_preserves_attempt_created_number(self) -> None:
        run_id = new_id()
        attempt = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=_make_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            AttemptCreated(run_id=run_id, timestamp=_ts(3), task_id="intake",
                           attempt_id=attempt, attempt_number=1),
            TaskStarted(run_id=run_id, timestamp=_ts(4), task_id="intake",
                        attempt_id=attempt),
            TaskCompleted(run_id=run_id, timestamp=_ts(5), task_id="intake", output_values={}),
        ]
        state = reduce(events)
        assert state.tasks["intake"].attempts[attempt].attempt_number == 1


class TestUndeclaredStrategyFallback:
    """ADR-005 增补 3：运行时遇到未声明策略的写入禁止静默覆盖已合并值——
    降级为 conflict_preserving 并落 ConflictRecord（发布期校验只覆盖已发布图，
    经 API/planner 进入的未发布图由 reducer 兜底）。"""

    def _no_strategy_graph(self) -> TaskGraph:
        """并行 r_a/r_b 对共享字段零策略声明（未发布图的 Planner 形态）。"""
        return TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "r_a": TaskGraphNode(
                    task_id="r_a", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "r_b": TaskGraphNode(
                    task_id="r_b", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
            },
            edges={"intake": [], "r_a": ["intake"], "r_b": ["intake"]},
        )

    def test_undeclared_parallel_write_does_not_silently_overwrite(self) -> None:
        run_id = new_id()
        a_id = new_id()
        b_id = new_id()
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=self._no_strategy_graph()),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_a"),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_b"),
            TaskStarted(run_id=run_id, timestamp=_ts(5), task_id="r_a", attempt_id=a_id),
            TaskStarted(run_id=run_id, timestamp=_ts(5), task_id="r_b", attempt_id=b_id),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(6), task_id="r_a",
                output_values={"finding_value": {"writer": "r_a"}},
            ),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(7), task_id="r_b",
                output_values={"finding_value": {"writer": "r_b"}},
            ),
        ]
        state = reduce(events)
        # 降级 conflict_preserving：canonical 不被后写者静默覆盖
        assert state.canonical["finding_value"] == {"writer": "r_a"}
        # 双方值并存于 ConflictRecord（不做仲裁）
        assert len(state.conflicts) == 1
        conflict = state.conflicts[0]
        assert conflict.field == "finding_value"
        assert set(conflict.values.keys()) == {"r_a", "r_b"}
        assert conflict.values == {"r_a": {"writer": "r_a"}, "r_b": {"writer": "r_b"}}

    def test_undeclared_single_writer_first_write_accepted(self) -> None:
        """单写者未声明字段：降级 CP 的 first-write 路径 = 正常落值（Planner
        fixture 图的行为保持——串行链字段名互不重叠时输出不变）。"""
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
            },
            edges={"intake": []},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(3), task_id="intake",
                output_values={"candidates": [{"id": 1}]},
            ),
        ]
        state = reduce(events)
        assert state.canonical["candidates"] == [{"id": 1}]
        assert state.conflicts == []

    def test_undeclared_overwrite_of_merged_value_becomes_conflict(self) -> None:
        """串行后写覆盖未声明字段的已合并值：同样禁止静默覆盖（增补 3 不区分
        并行/串行——写图者须显式声明 last_write_wins 才能表达覆盖语义）。"""
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "finalize": TaskGraphNode(
                    task_id="finalize", task_type="Fixture", dependencies=("intake",),
                    parallel_safe=False, required_capability="fixture", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
            },
            edges={"intake": [], "finalize": ["intake"]},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(3), task_id="intake",
                output_values={"status": "draft"},
            ),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="finalize"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(5), task_id="finalize",
                output_values={"status": "final"},
            ),
        ]
        state = reduce(events)
        assert state.canonical["status"] == "draft"
        assert len(state.conflicts) == 1
        conflict = state.conflicts[0]
        assert conflict.field == "status"
        assert conflict.values == {"intake": "draft", "finalize": "final"}

    def test_undeclared_write_after_append_does_not_take_over(self) -> None:
        """对抗审查 MAJOR（F-R1-01-R）：APPEND 先写后未声明写入——CP 分支
        prev_task=None fallback 曾静默接管已合并的追加列表（追加元素丢失、
        零冲突记录、事件面同样失明）。APPEND 首写必须登记 owner。"""
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "r_append": TaskGraphNode(
                    task_id="r_append", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"findings": MergeStrategy.APPEND},
                ),
                "r_rogue": TaskGraphNode(
                    task_id="r_rogue", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
            },
            edges={"intake": [], "r_append": ["intake"], "r_rogue": ["intake"]},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_append"),
            TaskScheduled(run_id=run_id, timestamp=_ts(4), task_id="r_rogue"),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(5), task_id="r_append",
                output_values={"findings": [{"source": "r_append"}]},
            ),
            TaskCompleted(
                run_id=run_id, timestamp=_ts(6), task_id="r_rogue",
                output_values={"findings": "rogue-scalar"},
            ),
        ]
        state = reduce(events)
        # 追加列表不被未声明后写静默接管（元素零丢失）
        assert state.canonical["findings"] == [{"source": "r_append"}]
        assert len(state.conflicts) == 1
        conflict = state.conflicts[0]
        assert conflict.field == "findings"
        assert conflict.values == {
            "r_append": [{"source": "r_append"}],
            "r_rogue": "rogue-scalar",
        }


def _interleave(seqs: list[list]) -> list[list]:
    """生成保留各子序列内部顺序的全部交错排列（0 依赖，穷举）。"""
    if not any(seqs):
        return [[]]
    results: list[list] = []
    for i, seq in enumerate(seqs):
        if seq:
            for rest in _interleave([*seqs[:i], seq[1:], *seqs[i + 1:]]):
                results.append([seq[0], *rest])
    return results


class TestAppendMergeOrdering:
    """ADR-005 增补 1：append 保序 = (task_id, attempt_no) 的纯函数——
    合并序列与事件到达/完成顺序无关，同一逻辑图两次执行（含并行调度抖动
    后的重放）必须产出逐字节相同的合并序列；同一 (task_id, attempt_no) 的
    重复投递按幂等去重。"""

    def _append_graph(self, branch_ids: tuple[str, ...]) -> TaskGraph:
        return TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"evidence_refs": MergeStrategy.APPEND},
                ),
                **{
                    tid: TaskGraphNode(
                        task_id=tid, task_type="Retrieve", dependencies=("intake",),
                        parallel_safe=True, required_capability="retrieve", budget={},
                        failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                        output_merge_strategies={"evidence_refs": MergeStrategy.APPEND},
                    )
                    for tid in branch_ids
                },
            },
            edges={"intake": [], **{tid: ["intake"] for tid in branch_ids}},
        )

    def _branch_events(
        self, run_id, task_id: str, values: list, offset: int,
        field: str = "evidence_refs",
    ) -> list:
        ts = _ts(offset)
        return [
            TaskScheduled(run_id=run_id, timestamp=ts, task_id=task_id),
            TaskStarted(
                run_id=run_id, timestamp=ts, task_id=task_id, attempt_id=new_id()
            ),
            TaskCompleted(
                run_id=run_id, timestamp=ts, task_id=task_id,
                output_values={field: values},
            ),
        ]

    def test_append_merge_ordering_independent_of_arrival(self) -> None:
        """到达序与 task-id 序相反：r_b 先完成，合并序仍由 (task_id, attempt_no) 决定。"""
        run_id = new_id()
        graph = self._append_graph(("r_a", "r_b"))
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            *self._branch_events(run_id, "r_b", ["from_b"], 4),
            *self._branch_events(run_id, "r_a", ["from_a"], 5),
        ]
        state = reduce(events)
        assert state.canonical["evidence_refs"] == ["from_a", "from_b"]

    def test_append_merge_pure_function_of_writer_set(self) -> None:
        """MR 排列性质（手写穷举 0 依赖）：同一事件集任意到达排列，reduce
        结果逐字节相同（sort_keys JSON 规约 dict 键序伪差）。"""
        import json

        branches = ("r_a", "r_b", "r_c")
        run_id = new_id()
        graph = self._append_graph(branches)
        prefix = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
        ]
        seqs = [
            self._branch_events(run_id, tid, [f"value_{tid}"], 4 + i)
            for i, tid in enumerate(branches)
        ]
        expected: str | None = None
        count = 0
        for merged in _interleave(seqs):
            state = reduce([*prefix, *merged])
            dump = json.dumps(state.model_dump(mode="json"), sort_keys=True)
            if expected is None:
                expected = dump
                assert state.canonical["evidence_refs"] == [
                    "value_r_a", "value_r_b", "value_r_c",
                ]
            assert dump == expected, f"排列 {count} 破坏确定性"
            count += 1
        assert count == 1680, "3 分支 × 3 事件的合法交错应恰 1680 种"

    def test_append_duplicate_delivery_deduped(self) -> None:
        """同一 (task_id, attempt_no) 重复投递幂等去重；不同写者键不合并。

        RED 修订登记（GREEN 0b5bc4d 后）：原断言误将 _apply_merge_field 当
        原地变更——其契约是返回合并值由调用方赋值（同文件既有 LWW/CP 用法
        同型），调用惯例修正，断言期望零变化。
        """
        from zhiwei.runtime.reducer import _apply_merge_field

        canonical: dict = {}
        owners: dict = {}
        writers: dict = {}
        canonical["f"] = _apply_merge_field(
            canonical, "f", ["x"], "t1", MergeStrategy.APPEND, owners,
            attempt_no=1, append_writers=writers,
        )[0]
        # 重复投递：同 (task_id, attempt_no) → 不双计
        canonical["f"] = _apply_merge_field(
            canonical, "f", ["x"], "t1", MergeStrategy.APPEND, owners,
            attempt_no=1, append_writers=writers,
        )[0]
        assert canonical["f"] == ["x"]
        # 不同 task 的写者键 → 正常合并且按 (task_id, attempt_no) 有序
        canonical["f"] = _apply_merge_field(
            canonical, "f", ["y"], "t0", MergeStrategy.APPEND, owners,
            attempt_no=3, append_writers=writers,
        )[0]
        assert canonical["f"] == ["y", "x"]
        # 同 task 不同 attempt_no → 不同写者键（重试语义）
        canonical["f"] = _apply_merge_field(
            canonical, "f", ["z"], "t1", MergeStrategy.APPEND, owners,
            attempt_no=2, append_writers=writers,
        )[0]
        assert canonical["f"] == ["y", "x", "z"]

    def test_lww_replacement_invalidates_append_registry(self) -> None:
        """B2 审查 MAJOR 根因②：声明 LWW 的并行写者覆盖 APPEND 字段（校验器
        修复前为合法图形态）——替换后写者注册表必须失效：不崩溃、无陈旧键。"""
        run_id = new_id()
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "tB": TaskGraphNode(
                    task_id="tB", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.APPEND},
                ),
                "t0": TaskGraphNode(
                    task_id="t0", task_type="Analyze", dependencies=("intake",),
                    parallel_safe=True, required_capability="analyze", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.LAST_WRITE_WINS},
                ),
                "tC": TaskGraphNode(
                    task_id="tC", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.APPEND},
                ),
            },
            edges={"intake": [], "tB": ["intake"], "t0": ["intake"], "tC": ["intake"]},
        )
        events = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
            *self._branch_events(run_id, "tB", ["b1"], 4, field="f"),
            *self._branch_events(run_id, "t0", ["lww"], 5, field="f"),
            *self._branch_events(run_id, "tC", ["c1"], 6, field="f"),
        ]
        state = reduce(events)
        # LWW（t0 < tB，task-id 序）替换追加列表后：注册表失效、后续 APPEND
        # 以哨兵键接续（"lww" 无写者身份排最前，c1 按 task-id 序在后）。
        # 注册表与列表长度平行——哨兵键保留（无写者身份的元素占位）。
        assert state.canonical["f"] == ["lww", "c1"]
        assert state.append_writers.get("f", ()) == (("", -1), ("tC", 1))

    def test_mixed_strategy_interleavings_do_not_crash(self) -> None:
        """同一混合策略事件集的任意到达排列：reduce 完成且无异常（崩溃即
        RED——GREEN 引入 zip(strict=True) 后 LWW 收缩方向未处理）。"""
        run_id = new_id()
        graph = self._hetero_graph()
        prefix = [
            RunCreated(run_id=run_id, timestamp=_ts(0), graph=graph),
            RunStarted(run_id=run_id, timestamp=_ts(1)),
            TaskScheduled(run_id=run_id, timestamp=_ts(2), task_id="intake"),
            TaskCompleted(run_id=run_id, timestamp=_ts(3), task_id="intake", output_values={}),
        ]
        seqs = [
            self._branch_events(run_id, "tB", ["b1", "b2"], 4, field="f"),
            self._branch_events(run_id, "t0", ["lww"], 5, field="f"),
            self._branch_events(run_id, "tC", ["c1"], 6, field="f"),
        ]
        for merged in _interleave(seqs):
            state = reduce([*prefix, *merged])
            assert isinstance(state.canonical.get("f", []), list)

    def _hetero_graph(self) -> TaskGraph:
        return TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", dependencies=(),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                ),
                "tB": TaskGraphNode(
                    task_id="tB", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.APPEND},
                ),
                "t0": TaskGraphNode(
                    task_id="t0", task_type="Analyze", dependencies=("intake",),
                    parallel_safe=True, required_capability="analyze", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.LAST_WRITE_WINS},
                ),
                "tC": TaskGraphNode(
                    task_id="tC", task_type="Retrieve", dependencies=("intake",),
                    parallel_safe=True, required_capability="retrieve", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={"f": MergeStrategy.APPEND},
                ),
            },
            edges={"intake": [], "tB": ["intake"], "t0": ["intake"], "tC": ["intake"]},
        )
