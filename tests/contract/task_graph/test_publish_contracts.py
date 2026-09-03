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
from zhiwei.agents.versions import (
    AgentVersionManager,
    PackVersionManager,
    VersionStateError,
)


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


class TestDelegationBoundary:
    """S2 修复轮批次 B RED（H-7）：委托终止界的发布期执行（ADR-008 可判定化增补）。

    委托依赖图（delegate 依赖 + agent-as-tool 引用 + SolutionPack 依赖）必须为
    DAG；任何环在发布边界被拒。此前发布边界只做单图 DAG/merge 校验——跨
    AgentVersion 的委托环（A→B→A、经 tool provider 交替）完全不设防
    （ADR-012 反例 3）。
    """

    def _draft(self, manager: AgentVersionManager, name: str):
        return manager.create_draft(name, "delegation boundary probe", ("fixture",))

    def test_delegate_cycle_rejected_at_publish(self) -> None:
        """A→B（delegate）+ B→A（delegate）= 环：promote 被拒。"""
        manager = AgentVersionManager()
        a = self._draft(manager, "agent-a")
        b = self._draft(manager, "agent-b")
        manager.set_delegation(a.id, delegate_dependencies=(b.id,))
        manager.set_delegation(b.id, delegate_dependencies=(a.id,))
        with pytest.raises(VersionStateError, match="delegation"):
            manager.promote_to_sandbox(a.id)

    def test_agent_as_tool_alternate_cycle_rejected_at_publish(self) -> None:
        """A→B（delegate）+ B→A（agent-as-tool 引用）交替成环：promote 被拒。

        交替使用两条委托路径绕过界正是 ADR-008 明令禁止的形态。
        """
        manager = AgentVersionManager()
        a = self._draft(manager, "agent-a")
        b = self._draft(manager, "agent-b")
        manager.set_delegation(a.id, delegate_dependencies=(b.id,))
        manager.set_delegation(b.id, tool_agent_refs=(a.id,))
        with pytest.raises(VersionStateError, match="delegation"):
            manager.promote_to_sandbox(b.id)

    def test_self_delegation_requires_declared_depth_cap(self) -> None:
        """自委托必须显式声明深度上限；声明后发布放行。"""
        manager = AgentVersionManager()
        a = self._draft(manager, "agent-a")
        manager.set_delegation(a.id, delegate_dependencies=(a.id,))
        with pytest.raises(VersionStateError, match="self-delegation"):
            manager.promote_to_sandbox(a.id)

        manager2 = AgentVersionManager()
        declared = self._draft(manager2, "agent-a")
        manager2.set_delegation(
            declared.id,
            delegate_dependencies=(declared.id,),
            self_delegation_depth_cap=2,
        )
        promoted = manager2.promote_to_sandbox(declared.id)
        assert promoted.status.value == "sandbox"

    def test_pack_dependency_cycle_rejected_at_publish(self) -> None:
        """A 的 solution pack 依赖 B 的 pack（B→A）+ A delegate 到 B（A→B）= 跨实体环。"""
        agents = AgentVersionManager()
        packs = PackVersionManager()
        agents.bind_pack_manager(packs)
        packs.bind_agent_manager(agents)

        a = self._draft(agents, "agent-a")
        b = self._draft(agents, "agent-b")
        agents.promote_to_sandbox(a.id)  # a 尚无委托 → 放行
        pack_a = packs.create_draft("pack-a", a.id, {})
        packs.promote_to_sandbox(pack_a.id)
        packs.promote_to_published(pack_a.id)
        # 依赖已发布的 pack_a：运行 b 的 solution 会拉入 a 的 pack（b→a 派生边）
        pack_b = packs.create_draft("pack-b", b.id, {}, depends_on=(pack_a.id,))
        packs.promote_to_sandbox(pack_b.id)
        packs.promote_to_published(pack_b.id)

        # a delegate 到 b（a→b）+ pack 派生边 b→a → 跨实体环
        agents.set_delegation(a.id, delegate_dependencies=(b.id,))
        with pytest.raises(VersionStateError, match="delegation"):
            agents.promote_to_sandbox(b.id)


class TestSingleSidedMergeDeclaration:
    """S2 修复轮批次 C RED（ADR-005 增补：单边声明 = 拒绝）。

    字段被 K 个可能并行的节点写入时，K 个节点都必须声明 merge 策略——
    「至少一方声明即放行」会让未声明写者在运行时走静默覆盖路径。
    """

    def test_single_sided_declaration_rejected_at_publish(self) -> None:
        graph = TaskGraph(
            nodes={
                "a": _node(
                    "a", parallel=True,
                    merge={"decision": MergeStrategy.CONFLICT_PRESERVING},
                    outputs={"decision": "x"},
                ),
                "b": _node("b", parallel=True, outputs={"decision": "x"}),
            },
            edges={},
        )
        manager = AgentVersionManager()
        manager.create_draft("single-sided", "desc", ("fixture",), graph=graph)
        with pytest.raises(ValueError, match="merge"):
            graph.validate_dag()
