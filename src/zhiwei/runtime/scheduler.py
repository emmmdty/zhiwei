"""S2 runtime: task readiness scheduler。

事实源：design doc §4.1、S2-T2 plan。

Determines which tasks are ready to execute based on DAG dependencies and parallel safety flags.
"""

from __future__ import annotations

from zhiwei.agents.task_graph import TaskGraph


class SchedulerError(RuntimeError):
    """Scheduler encountered an error (e.g., invalid DAG)."""


class Scheduler:
    """Determines task readiness and parallel execution groups.

    Uses the TaskGraph's DAG structure to compute which tasks can run next.
    """

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph
        self._is_valid: bool | None = None

    @property
    def is_valid_dag(self) -> bool:
        """Check if the graph is a valid DAG (no cycles)."""
        if self._is_valid is None:
            try:
                self._graph.topological_sort()
                self._is_valid = True
            except ValueError:
                self._is_valid = False
        return self._is_valid

    def topological_sort(self) -> list[str]:
        """Return tasks in topological order.

        Raises SchedulerError if the graph contains a cycle.
        """
        try:
            return self._graph.topological_sort()
        except ValueError as exc:
            raise SchedulerError(str(exc)) from exc

    def ready_tasks(self, completed: set[str]) -> set[str]:
        """Return tasks whose dependencies are all in the completed set."""
        return self._graph.ready_tasks(completed)

    def parallel_safe_tasks(self, completed: set[str]) -> set[str]:
        """Return tasks that are ready and marked parallel_safe."""
        return self._graph.parallel_safe_tasks(completed)
