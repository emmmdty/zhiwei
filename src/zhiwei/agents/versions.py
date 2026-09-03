"""Version lifecycle management for AgentDefinition and SolutionPack.

Draft → Sandbox → Published transitions with immutability enforcement.
Invalid parent/pack references are rejected at promotion time; typed task
graphs are validated at the publish boundary (ADR-005: undeclared parallel
merge strategies fail at publish, not at runtime).

委托终止界（ADR-008 可判定化增补，2026-09-03）：发布期对委托依赖图
（delegate 依赖 + agent-as-tool 引用 + SolutionPack 依赖边）做环检测，
任何环发布失败；自委托必须显式声明深度上限。终止性由「DAG 无环 + 运行时
共享计数严格递减」的构造直接成立，无独立静态证明义务。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from zhiwei.agents.domain import (
    AgentDefinition,
    AgentDefinitionStatus,
    SolutionPack,
    SolutionPackStatus,
    TaskGraphSchema,
)
from zhiwei.agents.task_graph import TaskGraph
from zhiwei.contracts.identifiers import new_id


class VersionStateError(RuntimeError):
    """Invalid version state transition."""


class DelegationCycleError(VersionStateError):
    """委托依赖图存在环（ADR-008：发布期拒绝，不是烧完预算才止损）。"""


class SelfDelegationUndeclaredError(VersionStateError):
    """自委托未显式声明深度上限（ADR-008 第①层）。"""


class InvalidParentReferenceError(RuntimeError):
    """Parent version reference does not exist."""


class InvalidPackReferenceError(RuntimeError):
    """Pack reference does not exist or is not published."""


class AgentVersionManager:
    """Manages the lifecycle of AgentDefinition versions.

    Tracks draft, sandbox, and published states. Enforces immutability
    after publish and validates parent references.
    """

    def __init__(self) -> None:
        self._agents: dict[UUID, AgentDefinition] = {}
        # typed graph 按 agent 挂载：draft 期可迭代修改，promote 边界做发布期校验
        # （ADR-005：未声明 merge 策略的并行写在发布期拒绝，不是运行时才报错）。
        self._graphs: dict[UUID, TaskGraph] = {}
        # 委托依赖图的 pack 侧边（可选绑定：跨实体环需要 pack 依赖边参与检测）
        self._pack_manager: PackVersionManager | None = None

    def bind_pack_manager(self, pack_manager: PackVersionManager) -> None:
        """绑定 pack 管理器：发布期委托环检测纳入 SolutionPack 依赖边。"""
        self._pack_manager = pack_manager

    def create_draft(
        self,
        name: str,
        description: str,
        capabilities: tuple[str, ...],
        task_graph_schema: TaskGraphSchema | None = None,
        *,
        graph: TaskGraph | None = None,
    ) -> AgentDefinition:
        """Create a new draft agent definition."""
        now = datetime.now(UTC)
        schema = task_graph_schema or TaskGraphSchema(
            tasks={"step1": {"type": "prompt", "template": "Hello"}},
            edges={"step1": []},
        )
        agent = AgentDefinition(
            id=new_id(),
            name=name,
            description=description,
            version=1,
            capabilities=capabilities,
            task_graph_schema=schema,
            status=AgentDefinitionStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._agents[agent.id] = agent
        if graph is not None:
            self._graphs[agent.id] = graph
        return agent

    def set_delegation(
        self,
        agent_id: UUID,
        *,
        delegate_dependencies: tuple[UUID, ...] | None = None,
        tool_agent_refs: tuple[UUID, ...] | None = None,
        self_delegation_depth_cap: int | None = None,
    ) -> AgentDefinition:
        """draft 期迭代委托声明（发布后不可变）。

        None = 不改该维度；显式空 tuple = 清空。发布边界做环检测与
        自委托声明校验（ADR-008）。
        """
        agent = self._get_agent(agent_id)
        if agent.status == AgentDefinitionStatus.PUBLISHED:
            raise VersionStateError(
                f"delegation is immutable after publish (status: {agent.status})"
            )
        updated = agent.model_copy(
            update={
                "delegate_dependencies": (
                    delegate_dependencies
                    if delegate_dependencies is not None
                    else agent.delegate_dependencies
                ),
                "tool_agent_refs": (
                    tool_agent_refs if tool_agent_refs is not None else agent.tool_agent_refs
                ),
                "self_delegation_depth_cap": self_delegation_depth_cap,
                "updated_at": datetime.now(UTC),
            }
        )
        self._agents[agent_id] = updated
        return updated

    def _validate_graph_for_publish(self, agent_id: UUID) -> None:
        """发布边界校验：DAG、依赖引用、并行写 merge 策略与委托终止界。"""
        graph = self._graphs.get(agent_id)
        if graph is not None:
            try:
                graph.validate_dag()
            except ValueError as exc:
                raise VersionStateError(
                    f"task graph rejected at publish boundary: {exc}"
                ) from exc
        self._validate_delegation_for_publish(agent_id)

    def _validate_delegation_for_publish(self, agent_id: UUID) -> None:
        """ADR-008 第①层：委托依赖图环检测 + 自委托声明校验。"""
        agents: Mapping[UUID, AgentDefinition] = self._agents
        for dep in agents[agent_id].delegate_dependencies + agents[agent_id].tool_agent_refs:
            if dep not in agents:
                raise VersionStateError(
                    f"delegation dependency {dep} not found in registry (fail closed)"
                )
        self_declarations: dict[UUID, int | None] = {
            aid: agent.self_delegation_depth_cap for aid, agent in agents.items()
        }
        packs: Mapping[UUID, SolutionPack] = (
            self._pack_manager._packs if self._pack_manager is not None else {}
        )
        assert_no_delegation_cycle(agents, packs, self_declarations)

    def _get_agent(self, agent_id: UUID) -> AgentDefinition:
        """Get agent by ID, raises if not found."""
        if agent_id not in self._agents:
            raise InvalidParentReferenceError(f"Agent {agent_id} not found")
        return self._agents[agent_id]

    def promote_to_sandbox(self, agent_id: UUID) -> AgentDefinition:
        """Promote a draft agent to sandbox status."""
        agent = self._get_agent(agent_id)
        if agent.status != AgentDefinitionStatus.DRAFT:
            raise VersionStateError(
                f"Cannot promote from {agent.status} to sandbox; only draft can be promoted"
            )
        self._validate_graph_for_publish(agent_id)
        updated = agent.model_copy(
            update={
                "status": AgentDefinitionStatus.SANDBOX,
                "updated_at": datetime.now(UTC),
            }
        )
        self._agents[agent_id] = updated
        return updated

    def promote_to_published(self, agent_id: UUID) -> AgentDefinition:
        """Promote a sandbox agent to published status."""
        agent = self._get_agent(agent_id)
        if agent.status != AgentDefinitionStatus.SANDBOX:
            raise VersionStateError(
                f"Cannot promote from {agent.status} to published; only sandbox can be promoted"
            )
        self._validate_graph_for_publish(agent_id)
        updated = agent.model_copy(
            update={
                "status": AgentDefinitionStatus.PUBLISHED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._agents[agent_id] = updated
        return updated


class PackVersionManager:
    """Manages the lifecycle of SolutionPack versions.

    Tracks draft, sandbox, and published states. Validates pack references
    (depends_on) exist and are published before allowing promotion.
    """

    def __init__(self) -> None:
        self._packs: dict[UUID, SolutionPack] = {}
        self._agent_manager: AgentVersionManager | None = None

    def bind_agent_manager(self, agent_manager: AgentVersionManager) -> None:
        """绑定 agent 管理器：发布期委托环检测纳入 pack 派生边。"""
        self._agent_manager = agent_manager

    def create_draft(
        self,
        name: str,
        agent_definition_id: UUID,
        content: dict[str, Any],
        depends_on: tuple[UUID, ...] | None = None,
    ) -> SolutionPack:
        """Create a new draft solution pack."""
        now = datetime.now(UTC)
        deps = depends_on or ()
        for dep_id in deps:
            if dep_id not in self._packs:
                raise InvalidPackReferenceError(f"Dependency pack {dep_id} not found")
            dep_pack = self._packs[dep_id]
            if dep_pack.status != SolutionPackStatus.PUBLISHED:
                raise InvalidPackReferenceError(
                    f"Dependency pack {dep_id} is not published (status: {dep_pack.status})"
                )
        pack = SolutionPack(
            id=new_id(),
            name=name,
            version=1,
            agent_definition_id=agent_definition_id,
            content=content,
            depends_on=deps,
            status=SolutionPackStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._packs[pack.id] = pack
        return pack

    def _get_pack(self, pack_id: UUID) -> SolutionPack:
        """Get pack by ID, raises if not found."""
        if pack_id not in self._packs:
            raise InvalidParentReferenceError(f"Pack {pack_id} not found")
        return self._packs[pack_id]

    def promote_to_sandbox(self, pack_id: UUID) -> SolutionPack:
        """Promote a draft pack to sandbox status."""
        pack = self._get_pack(pack_id)
        if pack.status != SolutionPackStatus.DRAFT:
            raise VersionStateError(
                f"Cannot promote from {pack.status} to sandbox; only draft can be promoted"
            )
        self._validate_delegation_for_publish(pack_id)
        updated = pack.model_copy(
            update={
                "status": SolutionPackStatus.SANDBOX,
                "updated_at": datetime.now(UTC),
            }
        )
        self._packs[pack_id] = updated
        return updated

    def promote_to_published(self, pack_id: UUID) -> SolutionPack:
        """Promote a sandbox pack to published status."""
        pack = self._get_pack(pack_id)
        if pack.status != SolutionPackStatus.SANDBOX:
            raise VersionStateError(
                f"Cannot promote from {pack.status} to published; only sandbox can be promoted"
            )
        self._validate_delegation_for_publish(pack_id)
        updated = pack.model_copy(
            update={
                "status": SolutionPackStatus.PUBLISHED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._packs[pack_id] = updated
        return updated

    def _validate_delegation_for_publish(self, pack_id: UUID) -> None:
        """pack 发布边界同样做委托环检测（pack 派生边参与组合图）。"""
        if self._agent_manager is None:
            return
        agents: Mapping[UUID, AgentDefinition] = self._agent_manager._agents
        packs: Mapping[UUID, SolutionPack] = self._packs
        self_declarations: dict[UUID, int | None] = {
            aid: agent.self_delegation_depth_cap for aid, agent in agents.items()
        }
        assert_no_delegation_cycle(agents, packs, self_declarations)


def _combined_delegation_edges(
    agents: Mapping[UUID, AgentDefinition],
    packs: Mapping[UUID, SolutionPack],
) -> dict[UUID, set[UUID]]:
    """组合委托依赖图的邻接表（agent 级）。

    边语义「执行 X 可能调用 Y」：
    - 直接边：X.delegate_dependencies ∪ X.tool_agent_refs（Delegate 与
      agent-as-tool 两条路径进同一张图，交替使用无法绕过检测）；
    - pack 派生边：pack(X).depends_on(pack(Y)) → X→Y（运行 X 的 solution
      会拉入 Y 的 pack）。
    声明了深度上限的自环不计入（运行时共享计数严格递减兜底，ADR-008 第②层）。
    """
    adjacency: dict[UUID, set[UUID]] = {aid: set() for aid in agents}
    for aid, agent in agents.items():
        edges = set(agent.delegate_dependencies) | set(agent.tool_agent_refs)
        edges.discard(aid)  # 自环单独按声明校验，不进环检测
        adjacency[aid] |= edges
    for pack in packs.values():
        owner = pack.agent_definition_id
        if owner not in adjacency:
            continue
        for dep_id in pack.depends_on:
            dep_pack = packs.get(dep_id)
            if dep_pack is not None and dep_pack.agent_definition_id in adjacency:
                adjacency[owner].add(dep_pack.agent_definition_id)
    return adjacency


def assert_no_delegation_cycle(
    agents: Mapping[UUID, AgentDefinition],
    packs: Mapping[UUID, SolutionPack],
    self_declarations: Mapping[UUID, int | None],
) -> None:
    """ADR-008 第①层：委托依赖图必须为 DAG；自委托必须显式声明深度上限。"""
    for aid, agent in agents.items():
        self_refs = {aid} & (
            set(agent.delegate_dependencies) | set(agent.tool_agent_refs)
        )
        if self_refs and self_declarations.get(aid) is None:
            raise SelfDelegationUndeclaredError(
                f"self-delegation on agent {aid} requires an explicit depth cap "
                "(ADR-008 layer 1)"
            )

    adjacency = _combined_delegation_edges(agents, packs)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[UUID, int] = dict.fromkeys(adjacency, WHITE)

    def _visit(node: UUID, path: list[UUID]) -> None:
        color[node] = GRAY
        for neighbor in sorted(adjacency[node]):
            state = color.get(neighbor, BLACK)
            if state == GRAY:
                cycle = [*path[path.index(neighbor):], neighbor]
                raise DelegationCycleError(
                    "delegation cycle detected at publish boundary (ADR-008): "
                    " -> ".join(str(hop) for hop in cycle)
                )
            if state == WHITE:
                _visit(neighbor, [*path, neighbor])
        color[node] = BLACK

    for node in sorted(adjacency):
        if color[node] == WHITE:
            _visit(node, [node])
