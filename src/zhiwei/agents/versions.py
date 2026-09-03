"""Version lifecycle management for AgentDefinition and SolutionPack.

Draft → Sandbox → Published transitions with immutability enforcement.
Invalid parent/pack references are rejected at promotion time; typed task
graphs are validated at the publish boundary (ADR-005: undeclared parallel
merge strategies fail at publish, not at runtime).
"""

from __future__ import annotations

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

    def _validate_graph_for_publish(self, agent_id: UUID) -> None:
        """发布边界校验：DAG、依赖引用与并行写 merge 策略声明。"""
        graph = self._graphs.get(agent_id)
        if graph is None:
            return
        try:
            graph.validate_dag()
        except ValueError as exc:
            raise VersionStateError(
                f"task graph rejected at publish boundary: {exc}"
            ) from exc

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
        updated = pack.model_copy(
            update={
                "status": SolutionPackStatus.PUBLISHED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._packs[pack_id] = updated
        return updated
