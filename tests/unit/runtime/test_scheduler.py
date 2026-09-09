"""S2-T2 RED: Task readiness scheduler tests."""

from __future__ import annotations

import pytest

from zhiwei.agents.task_graph import FailurePolicy, TaskGraph, TaskGraphNode
from zhiwei.runtime.scheduler import Scheduler, SchedulerError


def _make_simple_graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "intake": TaskGraphNode(
                task_id="intake", task_type="Intake", dependencies=(),
                parallel_safe=False, required_capability="intake", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
            "plan": TaskGraphNode(
                task_id="plan", task_type="Plan", dependencies=("intake",),
                parallel_safe=False, required_capability="plan", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
            "retrieve": TaskGraphNode(
                task_id="retrieve", task_type="Retrieve", dependencies=("plan",),
                parallel_safe=True, required_capability="retrieve", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
        },
        edges={
            "intake": [],
            "plan": ["intake"],
            "retrieve": ["plan"],
        },
    )


def _make_parallel_graph() -> TaskGraph:
    return TaskGraph(
        nodes={
            "intake": TaskGraphNode(
                task_id="intake", task_type="Intake", dependencies=(),
                parallel_safe=False, required_capability="intake", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
            "r_a": TaskGraphNode(
                task_id="r_a", task_type="Retrieve", dependencies=("intake",),
                parallel_safe=True, required_capability="retrieve", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
            "r_b": TaskGraphNode(
                task_id="r_b", task_type="Retrieve", dependencies=("intake",),
                parallel_safe=True, required_capability="retrieve", budget={},
                failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                output_merge_strategies={},
            ),
            "synth": TaskGraphNode(
                task_id="synth", task_type="Synthesize",
                dependencies=("r_a", "r_b"),
                parallel_safe=False, required_capability="synthesize", budget={},
                failure_policy=FailurePolicy.CANCEL, completion_obligations=(),
                output_merge_strategies={},
            ),
        },
        edges={
            "intake": [],
            "r_a": ["intake"],
            "r_b": ["intake"],
            "synth": ["r_a", "r_b"],
        },
    )


class TestSchedulerReadyTasks:
    def test_root_tasks_ready_when_no_deps(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        completed: set[str] = set()
        ready = scheduler.ready_tasks(completed)
        assert "intake" in ready
        assert "plan" not in ready
        assert "retrieve" not in ready

    def test_dependent_task_becomes_ready_when_deps_satisfied(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        completed = {"intake"}
        ready = scheduler.ready_tasks(completed)
        assert "plan" in ready
        assert "retrieve" not in ready

    def test_task_not_ready_until_all_deps_satisfied(self) -> None:
        graph = _make_parallel_graph()
        scheduler = Scheduler(graph)
        completed = {"intake", "r_a"}
        ready = scheduler.ready_tasks(completed)
        assert "synth" not in ready

    def test_task_ready_when_all_deps_satisfied(self) -> None:
        graph = _make_parallel_graph()
        scheduler = Scheduler(graph)
        completed = {"intake", "r_a", "r_b"}
        ready = scheduler.ready_tasks(completed)
        assert "synth" in ready

    def test_empty_completed_returns_only_root_tasks(self) -> None:
        graph = _make_parallel_graph()
        scheduler = Scheduler(graph)
        ready = scheduler.ready_tasks(set())
        assert ready == {"intake"}

    def test_all_completed_returns_empty(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        all_tasks = set(graph.nodes.keys())
        ready = scheduler.ready_tasks(all_tasks)
        assert ready == set()


class TestSchedulerParallelSafety:
    def test_parallel_safe_tasks_are_identified(self) -> None:
        graph = _make_parallel_graph()
        scheduler = Scheduler(graph)
        parallel = scheduler.parallel_safe_tasks({"intake"})
        assert parallel == {"r_a", "r_b"}

    def test_non_parallel_safe_tasks_not_in_parallel_set(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        parallel = scheduler.parallel_safe_tasks({"intake"})
        assert "plan" not in parallel


class TestSchedulerCycleDetection:
    def test_valid_dag_passes(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        assert scheduler.is_valid_dag

    def test_cycle_detected(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": TaskGraphNode(
                    task_id="a", task_type="Intake", dependencies=("b",),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={},
                ),
                "b": TaskGraphNode(
                    task_id="b", task_type="Plan", dependencies=("a",),
                    parallel_safe=False, required_capability="plan", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={},
                ),
            },
            edges={"a": ["b"], "b": ["a"]},
        )
        scheduler = Scheduler(graph)
        assert not scheduler.is_valid_dag

    def test_self_loop_detected(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": TaskGraphNode(
                    task_id="a", task_type="Intake", dependencies=("a",),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={},
                ),
            },
            edges={"a": ["a"]},
        )
        scheduler = Scheduler(graph)
        assert not scheduler.is_valid_dag


class TestSchedulerTopologicalSort:
    def test_topological_sort_respects_ordering(self) -> None:
        graph = _make_simple_graph()
        scheduler = Scheduler(graph)
        order = scheduler.topological_sort()
        assert order.index("intake") < order.index("plan")
        assert order.index("plan") < order.index("retrieve")

    def test_topological_sort_covers_all_tasks(self) -> None:
        graph = _make_parallel_graph()
        scheduler = Scheduler(graph)
        order = scheduler.topological_sort()
        assert set(order) == set(graph.nodes.keys())

    def test_topological_sort_invalid_dag_raises(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": TaskGraphNode(
                    task_id="a", task_type="Intake", dependencies=("b",),
                    parallel_safe=False, required_capability="intake", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={},
                ),
                "b": TaskGraphNode(
                    task_id="b", task_type="Plan", dependencies=("a",),
                    parallel_safe=False, required_capability="plan", budget={},
                    failure_policy=FailurePolicy.RETRY, completion_obligations=(),
                    output_merge_strategies={},
                ),
            },
            edges={"a": ["b"], "b": ["a"]},
        )
        scheduler = Scheduler(graph)
        with pytest.raises(SchedulerError):
            scheduler.topological_sort()


class TestRootTaskWithoutEdgesEntry:
    """S2 收口 RED：edges 缺省的根节点必须可达 ready（真实 workflow 暴露的缺陷）。

    TaskGraph.ready_tasks 只遍历 edges.items()，未在 edges 注册的根节点永远不 ready，
    导致 workflow 立即判 failed。真实 Temporal 集成测试（intake 无 edges 条目）触发。
    """

    def test_root_task_missing_from_edges_is_ready(self) -> None:
        graph = TaskGraph(
            nodes={
                "intake": TaskGraphNode(
                    task_id="intake", task_type="Intake", required_capability="intake",
                ),
                "analyze": TaskGraphNode(
                    task_id="analyze", task_type="Analyze",
                    dependencies=("intake",), required_capability="analyze",
                ),
            },
            edges={"analyze": ["intake"]},
        )
        scheduler = Scheduler(graph)
        assert scheduler.ready_tasks(set()) == {"intake"}
