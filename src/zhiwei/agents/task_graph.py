"""S2 agents: TaskGraph domain model for DAG validation, readiness, and parallel safety.

事实源：design doc §3.1、S2-T2 plan、ADR-005（parallel merge semantics）。

TaskGraph: directed acyclic graph of task nodes with typed I/O, merge strategy declarations,
and static validation at publish time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskPrimitive(StrEnum):
    """Fixed set of task primitives available in the runtime."""

    INTAKE = "Intake"
    PLAN = "Plan"
    CLARIFY = "Clarify"
    RETRIEVE = "Retrieve"
    ANALYZE = "Analyze"
    INVOKE_TOOL = "InvokeTool"
    DELEGATE = "Delegate"
    VERIFY = "Verify"
    REQUEST_APPROVAL = "RequestApproval"
    SYNTHESIZE = "Synthesize"
    EMIT_ARTIFACT = "EmitArtifact"
    WRITE_MEMORY_CANDIDATE = "WriteMemoryCandidate"
    FINISH = "Finish"


class MergeStrategy(StrEnum):
    """ADR-005 merge strategies for canonical state fields written by parallel tasks."""

    APPEND = "append"
    LAST_WRITE_WINS = "last_write_wins"
    CONFLICT_PRESERVING = "conflict_preserving"


class FailurePolicy(StrEnum):
    """Task failure handling policy."""

    RETRY = "retry"
    CANCEL = "cancel"
    ESCALATE = "escalate"


class TaskGraphNode(BaseModel):
    """A single task node in the task graph.

    Declares typed input/output, dependencies, parallel safety, required capability,
    budget, failure policy, completion obligations, and merge strategy declarations
    for each output field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    parallel_safe: bool = False
    required_capability: str = Field(min_length=1)
    budget: dict[str, Any] = Field(default_factory=dict)
    failure_policy: FailurePolicy = FailurePolicy.RETRY
    completion_obligations: tuple[str, ...] = ()
    output_merge_strategies: dict[str, MergeStrategy] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class TaskGraph(BaseModel):
    """Directed acyclic graph of task nodes.

    Validated at construction: all dependency references must exist in nodes,
    and the graph must not contain cycles.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: dict[str, TaskGraphNode] = Field(min_length=1)
    edges: dict[str, list[str]] = Field(default_factory=dict)

    def validate_dag(self) -> None:
        """Validate that the graph is a DAG (no cycles) and all edges reference existing nodes."""
        # Check all edge references point to existing nodes
        for task_id, deps in self.edges.items():
            if task_id not in self.nodes:
                raise ValueError(f"Edge references unknown task: {task_id}")
            for dep in deps:
                if dep not in self.nodes:
                    raise ValueError(
                        f"Task '{task_id}' depends on unknown task: {dep}"
                    )

        # Check node dependencies match edges
        for task_id, node in self.nodes.items():
            expected_deps = set(self.edges.get(task_id, []))
            actual_deps = set(node.dependencies)
            if expected_deps != actual_deps:
                raise ValueError(
                    f"Task '{task_id}' dependencies mismatch: "
                    f"edges={expected_deps}, node={actual_deps}"
                )

        # Cycle detection via Kahn's algorithm
        self.topological_sort()

        # ADR-005: parallel writes must declare merge strategies
        self.validate_merge_strategies()

    def topological_sort(self) -> list[str]:
        """Return tasks in topological order. Raises ValueError if cycle exists."""
        # Build in-degree map
        in_degree: dict[str, int] = dict.fromkeys(self.nodes, 0)
        for task_id, deps in self.edges.items():
            for _dep in deps:
                in_degree[task_id] += 1

        # Start with nodes that have no dependencies
        queue = sorted([tid for tid, deg in in_degree.items() if deg == 0])
        order: list[str] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            # Find all tasks that depend on this node
            for task_id, deps in self.edges.items():
                if node in deps:
                    in_degree[task_id] -= 1
                    if in_degree[task_id] == 0:
                        queue.append(task_id)
            queue.sort()  # Deterministic ordering

        if len(order) != len(self.nodes):
            raise ValueError("Graph contains a cycle")

        return order

    def ready_tasks(self, completed: set[str]) -> set[str]:
        """Return tasks whose dependencies are all in the completed set.

        遍历 nodes（不是 edges.items()）：未在 edges 注册的节点视为无依赖——
        否则根节点永远不 ready，workflow 会立即误判调度不可推进。
        """
        ready = set()
        for task_id, node in self.nodes.items():
            if task_id in completed:
                continue
            if all(dep in completed for dep in node.dependencies):
                ready.add(task_id)
        return ready

    def parallel_safe_tasks(self, completed: set[str]) -> set[str]:
        """Return tasks that are ready and marked parallel_safe."""
        return {
            tid
            for tid in self.ready_tasks(completed)
            if self.nodes[tid].parallel_safe
        }

    def _ancestors(self, task_id: str) -> set[str]:
        """Return all ancestors of a task (tasks it transitively depends on)."""
        visited: set[str] = set()
        queue = list(self.edges.get(task_id, []))
        while queue:
            tid = queue.pop()
            if tid in visited:
                continue
            visited.add(tid)
            queue.extend(self.edges.get(tid, []))
        return visited

    def validate_merge_strategies(self) -> None:
        """ADR-005: Validate that parallel tasks writing the same field declare merge strategies.

        A field written by two tasks with no dependency relationship between them
        must have a merge strategy declared in at least one of the tasks'
        output_merge_strategies.

        Raises ValueError if undeclared parallel writes exist.
        """
        # Collect output fields per task
        task_fields: dict[str, set[str]] = {}
        for task_id, node in self.nodes.items():
            fields = set(node.output_merge_strategies.keys()) | set(node.output_schema.keys())
            if fields:
                task_fields[task_id] = fields

        # Check each pair of tasks for parallel writes without merge strategies
        task_ids = sorted(self.nodes.keys())
        checked: set[tuple[str, str, str]] = set()  # (a, b, field)

        for i, a in enumerate(task_ids):
            a_ancestors = self._ancestors(a)
            for b in task_ids[i + 1:]:
                # Tasks are parallel if neither is an ancestor of the other
                if b in a_ancestors or a in self._ancestors(b):
                    continue
                a_fields = task_fields.get(a, set())
                b_fields = task_fields.get(b, set())
                shared = a_fields & b_fields
                for field in shared:
                    key = (a, b, field)
                    if key in checked:
                        continue
                    checked.add(key)
                    a_has_strategy = field in self.nodes[a].output_merge_strategies
                    b_has_strategy = field in self.nodes[b].output_merge_strategies
                    if not a_has_strategy and not b_has_strategy:
                        raise ValueError(
                            f"Parallel tasks '{a}' and '{b}' write to field "
                            f"'{field}' without any declared merge strategy"
                        )
