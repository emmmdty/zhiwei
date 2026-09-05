"""S10 冻结契约：Studio 受约束 Task Graph 校验语义（A 档，S10-T2）。

Task editor 只允许 Core primitives 与 typed ports，实时校验 DAG、capability、
input/output、budget、completion obligations。本文件冻结校验的语义与 issue 代码；
实现必须满足全部断言，GREEN 阶段不得修改本文件。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiwei.agents.task_graph import (
    MergeStrategy,
    TaskGraph,
    TaskGraphNode,
    validate_studio_graph,
)

KNOWN_CAPABILITIES = frozenset({"knowledge.retrieve@1", "github.read@1"})


def _node(
    task_id: str = "t1",
    *,
    task_type: str = "Retrieve",
    capability: str = "knowledge.retrieve@1",
    dependencies: tuple[str, ...] = (),
    budget: dict[str, object] | None = None,
    obligations: tuple[str, ...] = (),
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
    merge: dict[str, MergeStrategy] | None = None,
) -> TaskGraphNode:
    return TaskGraphNode(
        task_id=task_id,
        task_type=task_type,
        dependencies=dependencies,
        required_capability=capability,
        budget=budget or {},
        completion_obligations=obligations,
        input_schema=input_schema or {},
        output_schema=output_schema or {},
        output_merge_strategies=merge or {},
    )


def _issues(graph: TaskGraph) -> dict[tuple[str, str], str]:
    result = validate_studio_graph(graph, declared_capabilities=KNOWN_CAPABILITIES)
    return {(i.task_id, i.code): i.field for i in result}


class TestPrimitiveAndCapability:
    def test_unknown_primitive_flagged(self) -> None:
        graph = TaskGraph(tasks=[_node(task_type="Teleport")], edges=[])
        assert _issues(graph)[("t1", "unknown_primitive")] == "task_type"

    def test_known_primitive_clean(self) -> None:
        graph = TaskGraph(tasks=[_node()], edges=[])
        assert validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        ) == ()

    def test_unknown_capability_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[_node(capability="repo.destroy@9")], edges=[]
        )
        assert _issues(graph)[("t1", "unknown_capability")] == "required_capability"


class TestBudget:
    def test_unknown_budget_key_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[_node(budget={"free_tokens": 100})], edges=[]
        )
        assert _issues(graph)[("t1", "unknown_budget_key")] == "budget"

    def test_non_positive_budget_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[_node(budget={"max_model_calls": 0})], edges=[]
        )
        assert _issues(graph)[("t1", "invalid_budget")] == "budget"

    def test_valid_budget_clean(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(budget={"max_model_calls": 3, "max_tokens": 1000})
            ],
            edges=[],
        )
        assert validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        ) == ()


class TestObligations:
    def test_obligation_without_output_field_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(
                    obligations=("output:missing_field",),
                    output_schema={"properties": {"brief": {"type": "object"}}},
                )
            ],
            edges=[],
        )
        assert _issues(graph)[("t1", "unknown_obligation")] == "completion_obligations"

    def test_obligation_with_output_field_clean(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(
                    obligations=("output:brief",),
                    output_schema={"properties": {"brief": {"type": "object"}}},
                )
            ],
            edges=[],
        )
        assert validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        ) == ()


class TestTypedPorts:
    def test_unknown_port_type_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(output_schema={"properties": {"x": {"type": "quantum"}}})
            ],
            edges=[],
        )
        assert _issues(graph)[("t1", "unknown_port_type")] == "output_schema"

    def test_port_type_mismatch_flagged(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(
                    task_id="src",
                    output_schema={"properties": {"brief": {"type": "string"}}},
                ),
                _node(
                    task_id="dst",
                    dependencies=("src",),
                    input_schema={"properties": {"brief": {"type": "number"}}},
                ),
            ],
            edges=[("src", "dst")],
        )
        assert _issues(graph)[("dst", "port_type_mismatch")] == "input_schema"

    def test_matching_port_types_clean(self) -> None:
        graph = TaskGraph(
            tasks=[
                _node(
                    task_id="src",
                    output_schema={"properties": {"brief": {"type": "object"}}},
                ),
                _node(
                    task_id="dst",
                    dependencies=("src",),
                    input_schema={"properties": {"brief": {"type": "object"}}},
                ),
            ],
            edges=[("src", "dst")],
        )
        assert validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        ) == ()

    def test_undeclared_ports_not_flagged(self) -> None:
        # 只检查「已声明类型的同名端口」冲突；未声明/部分声明不猜测（运行时动态面）。
        graph = TaskGraph(
            tasks=[
                _node(task_id="src", output_schema={}),
                _node(
                    task_id="dst",
                    dependencies=("src",),
                    input_schema={"properties": {"brief": {"type": "object"}}},
                ),
            ],
            edges=[("src", "dst")],
        )
        assert validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        ) == ()


class TestDagStillEnforced:
    def test_cycle_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            TaskGraph(
                tasks=[
                    _node(task_id="a", dependencies=("b",)),
                    _node(task_id="b", dependencies=("a",)),
                ],
                edges=[("a", "b"), ("b", "a")],
            )


class TestIssueShape:
    def test_issue_is_frozen_and_machine_shaped(self) -> None:
        graph = TaskGraph(tasks=[_node(task_type="Teleport")], edges=[])
        (issue,) = validate_studio_graph(
            graph, declared_capabilities=KNOWN_CAPABILITIES
        )
        with pytest.raises(ValidationError):
            issue.code = "other"  # type: ignore[misc]
