"""S2 agents: TaskGraph domain model for DAG validation, readiness, and parallel safety.

事实源：design doc §3.1、S2-T2 plan、ADR-005（parallel merge semantics）。

TaskGraph: directed acyclic graph of task nodes with typed I/O, merge strategy declarations,
and static validation at publish time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    双构造形态：
    - ``nodes=``（dict + 依赖 dict）是 runtime/发布面的既有形态，构造期**不**做
      全量校验——S2 冻结契约（tests/contract/task_graph/test_publish_contracts.py）
      依赖「可构造非法图再显式 validate_dag()」的惰性行为；
    - ``tasks=``（节点列表 + edge 二元组列表）是 S10 Studio 编辑器的列表形态
      （tests/unit/agents/test_studio_validation_frozen.py 冻结）：构造期拒绝
      结构错误（环 / 未知依赖 / 依赖与边不一致），但 merge 策略仍留给发布期
      validate_dag——draft 是允许的中间态，Studio validate 只报告冻结的 issue 代码。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: dict[str, TaskGraphNode] = Field(min_length=1)
    edges: dict[str, list[str]] = Field(default_factory=dict)
    # Studio 列表形态的原始输入；nodes= 形态下保持空（两种形态互斥，见上方说明）。
    # exclude=True：tasks 只是构造期输入通道，nodes 才是权威投影——dump 带 tasks
    # 会让 workflow 输入的 model_validate 往返撞上互斥门（S2 runtime 回归实测）。
    tasks: tuple[TaskGraphNode, ...] = Field(default=(), exclude=True)

    def __init__(
        self,
        *,
        nodes: Mapping[str, TaskGraphNode] | None = None,
        tasks: Sequence[TaskGraphNode] | None = None,
        edges: Mapping[str, Sequence[str]] | Sequence[Sequence[str]] | None = None,
        **data: Any,
    ) -> None:
        """显式构造签名：nodes/tasks 与 edges 的 dict/list 两形态都对类型检查器
        可见（冻结契约以 tasks= + edge 二元组列表构造）。归一仍由下方
        _normalize_studio_shape 校验管线执行——本方法只做 kwargs 装配。"""
        if tasks is not None:
            if nodes is not None:
                raise ValueError("TaskGraph accepts either nodes or tasks, not both")
            data["tasks"] = tasks
        elif nodes is not None:
            data["nodes"] = nodes
        if edges is not None:
            data["edges"] = edges
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _normalize_studio_shape(cls, data: Any) -> Any:
        """把 Studio 列表形态归一为 nodes/edges dict 形态。

        edge 二元组 (src, dst) 语义为「dst 依赖 src」，与依赖 dict 的
        edges[task_id] = deps 方向一致；重复 task_id 拒绝（dict 静默覆盖会让
        两个节点之一凭空消失）。空 tasks 键不构成 Studio 形态：nodes= 形态的
        历史投影可能携带 tasks=[]，按 nodes= 处理。
        """
        if not isinstance(data, dict):
            return data
        if data.get("tasks"):
            if data.get("nodes") is not None:
                raise ValueError("TaskGraph accepts either nodes or tasks, not both")
            nodes: dict[str, TaskGraphNode] = {}
            for raw in data["tasks"]:
                node = raw if isinstance(raw, TaskGraphNode) else TaskGraphNode.model_validate(raw)
                if node.task_id in nodes:
                    raise ValueError(f"Duplicate task_id: {node.task_id}")
                nodes[node.task_id] = node
            data = {**data, "nodes": nodes}
        elif "tasks" in data:
            data = {key: value for key, value in data.items() if key != "tasks"}
        if isinstance(data.get("edges"), list):
            converted: dict[str, list[str]] = {}
            for pair in data["edges"]:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError(f"edge must be a (source, destination) pair: {pair!r}")
                src, dst = pair
                converted.setdefault(dst, []).append(src)
            data = {**data, "edges": converted}
        return data

    @model_validator(mode="after")
    def _validate_studio_shape(self) -> TaskGraph:
        if self.tasks:
            self._validate_references_and_dependencies()
            self.topological_sort()
        return self

    def validate_dag(self) -> None:
        """Validate that the graph is a DAG (no cycles) and all edges reference existing nodes."""
        self._validate_references_and_dependencies()

        # Cycle detection via Kahn's algorithm
        self.topological_sort()

        # ADR-005: parallel writes must declare merge strategies
        self.validate_merge_strategies()

    def _validate_references_and_dependencies(self) -> None:
        """边引用存在性 + 节点 dependencies 与 edges dict 逐点一致（DAG 判定的前半段）。"""
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
        """ADR-005（2026-09-03 增补）: 并行写同一字段的**每个写者**都必须声明 merge 策略。

        单边声明（一方声明、另一方未声明）同样拒绝：未声明写者在运行时走
        静默覆盖路径，正是 ADR-005 要在发布期消灭的 dict.update 事故。

        Raises ValueError if any potential parallel writer lacks a strategy.
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
                    if not (a_has_strategy and b_has_strategy):
                        raise ValueError(
                            f"Parallel tasks '{a}' and '{b}' write to field "
                            f"'{field}'; every potential writer must declare a "
                            f"merge strategy (single-sided declaration rejected)"
                        )


# ---------------------------------------------------------------------------
# S10 Studio 受约束编辑器的校验面（冻结契约：tests/unit/agents/
# test_studio_validation_frozen.py；issue 代码与 field 指向不得增删改名）。
# ---------------------------------------------------------------------------

STUDIO_BUDGET_KEYS: frozenset[str] = frozenset(
    {"max_model_calls", "max_tokens", "max_usd_micros"}
)
STUDIO_PORT_TYPES: frozenset[str] = frozenset(
    {"string", "number", "boolean", "object", "array", "ref"}
)

_CORE_PRIMITIVE_VALUES = frozenset(primitive.value for primitive in TaskPrimitive)


class StudioValidationIssue(BaseModel):
    """Studio 校验 issue：机器可读四元组（frozen——契约断言不可变性）。"""

    model_config = ConfigDict(frozen=True)

    code: str
    task_id: str
    field: str
    detail: str


def _schema_properties(schema: Any) -> dict[str, Any]:
    """取 schema 的 properties 映射；形状不合规返回空（端口检查按未声明处理）。"""
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return properties


def _declared_port_types(schema: Any) -> dict[str, str]:
    """已声明类型的端口（name -> type）；缺 type/非法形状的属性按未声明跳过。"""
    declared: dict[str, str] = {}
    for name, spec in _schema_properties(schema).items():
        if isinstance(spec, dict) and isinstance(spec.get("type"), str):
            declared[str(name)] = spec["type"]
    return declared


def validate_studio_graph(
    graph: TaskGraph, *, declared_capabilities: frozenset[str]
) -> tuple[StudioValidationIssue, ...]:
    """Studio 草稿校验：只报告冻结 issue 代码，不抛结构异常（构造期已拒绝）。

    校验语义（对应冻结测试分组）：
    - unknown_primitive：task_type 不在 13 个 Core 原语内；
    - unknown_capability：required_capability 不在 agent 记录的声明集内
      （声明集为空 → 全部 capability 未知，fail closed）；
    - unknown_budget_key / invalid_budget：budget 键 ⊆ 3 键词汇、值为正 int
      （bool 是 int 子类，显式排除）；未知键不重复报值错误；
    - unknown_obligation：completion obligation 必须是 "output:<field>" 且
      field 在节点 output_schema properties 内（其他前缀一律未知，不猜）；
    - unknown_port_type：已声明端口的 type 必须在 6 型词汇内；
    - port_type_mismatch：依赖边上同名端口 src output 与 dst input 类型不同
      ——issue 落在 dst 的 input_schema；任一侧未声明类型则不猜测（动态面）。

    确定性：节点按构造顺序、端口名按字典序遍历——同输入产出同 issue 序列。
    """
    issues: list[StudioValidationIssue] = []

    for task_id, node in graph.nodes.items():
        if node.task_type not in _CORE_PRIMITIVE_VALUES:
            issues.append(
                StudioValidationIssue(
                    code="unknown_primitive",
                    task_id=task_id,
                    field="task_type",
                    detail=(
                        f"task_type {node.task_type!r} is not a Core primitive; "
                        f"allowed: {', '.join(sorted(_CORE_PRIMITIVE_VALUES))}"
                    ),
                )
            )
        if node.required_capability not in declared_capabilities:
            issues.append(
                StudioValidationIssue(
                    code="unknown_capability",
                    task_id=task_id,
                    field="required_capability",
                    detail=(
                        f"capability {node.required_capability!r} is not declared "
                        "on this agent"
                    ),
                )
            )

        unknown_keys = sorted(set(node.budget) - STUDIO_BUDGET_KEYS)
        if unknown_keys:
            issues.append(
                StudioValidationIssue(
                    code="unknown_budget_key",
                    task_id=task_id,
                    field="budget",
                    detail=f"unknown budget keys: {', '.join(unknown_keys)}",
                )
            )
        invalid_values = sorted(
            f"{key}={node.budget[key]!r}"
            for key in STUDIO_BUDGET_KEYS & set(node.budget)
            if isinstance(node.budget[key], bool)
            or not isinstance(node.budget[key], int)
            or node.budget[key] <= 0
        )
        if invalid_values:
            issues.append(
                StudioValidationIssue(
                    code="invalid_budget",
                    task_id=task_id,
                    field="budget",
                    detail=(
                        "budget values must be positive integers; got: "
                        f"{', '.join(invalid_values)}"
                    ),
                )
            )

        output_properties = _schema_properties(node.output_schema)
        unknown_obligations = [
            obligation
            for obligation in node.completion_obligations
            if not (
                obligation.startswith("output:")
                and obligation[len("output:") :] in output_properties
            )
        ]
        if unknown_obligations:
            issues.append(
                StudioValidationIssue(
                    code="unknown_obligation",
                    task_id=task_id,
                    field="completion_obligations",
                    detail=(
                        "obligations must reference an output property as "
                        f"'output:<field>'; unresolved: {', '.join(unknown_obligations)}"
                    ),
                )
            )

        for field_name, schema in (
            ("input_schema", node.input_schema),
            ("output_schema", node.output_schema),
        ):
            offenders = sorted(
                f"{name!r} declares no known type"
                for name, spec in _schema_properties(schema).items()
                if not isinstance(spec, dict)
                or not isinstance(spec.get("type"), str)
                or spec["type"] not in STUDIO_PORT_TYPES
            )
            if offenders:
                issues.append(
                    StudioValidationIssue(
                        code="unknown_port_type",
                        task_id=task_id,
                        field=field_name,
                        detail=(
                            "port types must be one of "
                            f"{', '.join(sorted(STUDIO_PORT_TYPES))}; "
                            f"offenders: {'; '.join(offenders)}"
                        ),
                    )
                )

    for dst_id, deps in graph.edges.items():
        # nodes= 形态不保证构造期校验（见类 docstring）：悬空边在发布边界拒绝，
        # 不静默跳过（fail closed）。
        dst_node = graph.nodes.get(dst_id)
        if dst_node is None:
            raise ValueError(f"Edge references unknown task: {dst_id}")
        dst_inputs = _declared_port_types(dst_node.input_schema)
        for dep in deps:
            src_node = graph.nodes.get(dep)
            if src_node is None:
                raise ValueError(f"Task '{dst_id}' depends on unknown task: {dep}")
            src_outputs = _declared_port_types(src_node.output_schema)
            mismatched = sorted(set(src_outputs) & set(dst_inputs))
            for name in mismatched:
                src_type = src_outputs[name]
                dst_type = dst_inputs[name]
                if src_type != dst_type:
                    issues.append(
                        StudioValidationIssue(
                            code="port_type_mismatch",
                            task_id=dst_id,
                            field="input_schema",
                            detail=(
                                f"port {name!r}: task {dep!r} outputs "
                                f"{src_type!r} but task {dst_id!r} expects {dst_type!r}"
                            ),
                        )
                    )

    return tuple(issues)
