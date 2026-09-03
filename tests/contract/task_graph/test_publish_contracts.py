"""S2-T2 契约：TaskGraph 的发布期验证（ADR-005）。

事实源：specs/s2-agent-runtime.md §6（「未声明 merge 策略的字段在发布期即被拒绝」）、
ADR-005。契约：非法图在 promote（发布边界）被拒，而不是运行时才报错。
"""

from __future__ import annotations

import pytest

from zhiwei.agents.task_graph import (
    FailurePolicy,
    MergeStrategy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.agents.versions import AgentVersionManager, VersionStateError


def _node(task_id: str, *, deps: tuple[str, ...] = (), parallel: bool = False,
          merge: dict[str, MergeStrategy] | None = None,
          outputs: dict[str, object] | None = None) -> TaskGraphNode:
    return TaskGraphNode(
        task_id=task_id,
        task_type="Fixture",
        dependencies=deps,
        parallel_safe=parallel,
        required_capability="fixture",
        failure_policy=FailurePolicy.RETRY,
        output_merge_strategies=merge or {},
        output_schema={k: {"type": "string"} for k in (outputs or {})},
    )


class TestPublishBoundaryValidation:
    def test_undeclared_parallel_write_rejected_at_promotion(self) -> None:
        """两个并行节点写同一字段且未声明 merge 策略 → promote 被拒（发布期）。"""
        graph = TaskGraph(
            nodes={
                "a": _node("a", parallel=True, outputs={"decision": "x"}),
                "b": _node("b", parallel=True, outputs={"decision": "x"}),
            },
            edges={},
        )
        manager = AgentVersionManager()
        agent = manager.create_draft("bad", "desc", ("fixture",), graph=graph)
        with pytest.raises(VersionStateError, match="merge"):
            manager.promote_to_sandbox(agent.id)

    def test_declared_parallel_write_passes_promotion(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": _node("a", parallel=True, merge={"decision": MergeStrategy.CONFLICT_PRESERVING},
                           outputs={"decision": "x"}),
                "b": _node("b", parallel=True, outputs={"decision": "x"}),
            },
            edges={},
        )
        manager = AgentVersionManager()
        agent = manager.create_draft("good", "desc", ("fixture",), graph=graph)
        promoted = manager.promote_to_sandbox(agent.id)
        assert promoted.status.value == "sandbox"

    def test_cycle_rejected_at_promotion(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": _node("a", deps=("b",)),
                "b": _node("b", deps=("a",)),
            },
            edges={"a": ["b"], "b": ["a"]},
        )
        manager = AgentVersionManager()
        agent = manager.create_draft("cyclic", "desc", ("fixture",), graph=graph)
        with pytest.raises(VersionStateError, match=r"cycle|依赖|dependency"):
            manager.promote_to_sandbox(agent.id)

    def test_unknown_dependency_rejected_at_promotion(self) -> None:
        graph = TaskGraph(
            nodes={"a": _node("a", deps=("missing",))},
            edges={"a": ["missing"]},
        )
        manager = AgentVersionManager()
        agent = manager.create_draft("dangling", "desc", ("fixture",), graph=graph)
        with pytest.raises(VersionStateError):
            manager.promote_to_sandbox(agent.id)

    def test_publish_boundary_also_validates(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": _node("a", parallel=True, outputs={"f": "x"}),
                "b": _node("b", parallel=True, outputs={"f": "x"}),
            },
            edges={},
        )
        manager = AgentVersionManager()
        agent = manager.create_draft("bad2", "desc", ("fixture",), graph=graph)
        # sandbox 校验被绕过的场景不存在——draft 直接 promote_to_published 被
        # 状态机拒绝（只有 sandbox 能 published）；此处验证 published 边界同样
        # 校验图：先手工置为 sandbox 再 publish
        object.__setattr__(agent, "status", type(agent.status).SANDBOX)
        manager._agents[agent.id] = agent
        with pytest.raises(VersionStateError, match="merge"):
            manager.promote_to_published(agent.id)


class TestGraphSchemaContracts:
    def test_valid_graph_passes_validate_dag(self) -> None:
        graph = TaskGraph(
            nodes={
                "intake": _node("intake"),
                "a": _node("a", deps=("intake",), parallel=True,
                           merge={"obs": MergeStrategy.APPEND}, outputs={"obs": "x"}),
                "b": _node("b", deps=("intake",), parallel=True,
                           merge={"obs": MergeStrategy.APPEND}, outputs={"obs": "x"}),
                "final": _node("final", deps=("a", "b")),
            },
            edges={"a": ["intake"], "b": ["intake"], "final": ["a", "b"]},
        )
        graph.validate_dag()  # 不抛即通过

    def test_node_dependencies_must_match_edges(self) -> None:
        graph = TaskGraph(
            nodes={"a": _node("a", deps=("b",)), "b": _node("b")},
            edges={"a": []},
        )
        with pytest.raises(ValueError, match="dependencies mismatch"):
            graph.validate_dag()
