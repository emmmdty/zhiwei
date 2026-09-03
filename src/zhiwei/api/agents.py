"""S2 runtime: Agent CRUD API endpoints。

事实源：design doc §3.1、S2-T7 plan。

- GET /api/v1/agents — list agents
- POST /api/v1/agents — create agent
- GET /api/v1/agents/{agent_id} — get agent detail
- DELETE /api/v1/agents/{agent_id} — delete agent
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from zhiwei.agents.domain import AgentDefinition, AgentDefinitionStatus
from zhiwei.contracts.identifiers import new_id
from zhiwei.identity.domain import ActorContext


class CreateAgentRequest(BaseModel):
    """Request body for creating an agent definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    capabilities: list[str]
    task_graph_schema: dict[str, object]


class AgentRecord(BaseModel):
    """Agent record for API responses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    description: str
    version: int
    status: str
    capabilities: list[str]


class AgentsRepository:
    """In-memory agent storage for the API layer.

    All queries are scoped by organization_id for tenant isolation.
    """

    def __init__(self) -> None:
        self._agents: dict[tuple[UUID, UUID], AgentDefinition] = {}

    def add(self, agent: AgentDefinition, organization_id: UUID) -> None:
        self._agents[(organization_id, agent.id)] = agent

    def get(self, agent_id: UUID, organization_id: UUID) -> AgentDefinition | None:
        return self._agents.get((organization_id, agent_id))

    def list_all(self, organization_id: UUID) -> list[AgentDefinition]:
        return [agent for (org_id, _), agent in self._agents.items() if org_id == organization_id]

    def remove(self, agent_id: UUID, organization_id: UUID) -> bool:
        key = (organization_id, agent_id)
        if key in self._agents:
            del self._agents[key]
            return True
        return False


_agents_repo = AgentsRepository()


def create_agents_router(
    *,
    actor_dependency: Callable[[], ActorContext],
) -> APIRouter:
    """Create the agents API router."""
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

    @router.get("", response_model=list[AgentRecord])
    async def list_agents(
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[AgentRecord]:
        org_id = actor.organization_id
        if org_id is None:
            return []
        agents = _agents_repo.list_all(org_id)
        return [
            AgentRecord(
                id=a.id,
                name=a.name,
                description=a.description,
                version=a.version,
                status=a.status.value,
                capabilities=list(a.capabilities),
            )
            for a in agents
        ]

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=AgentRecord)
    async def create_agent(
        request: CreateAgentRequest,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> AgentRecord:
        org_id = actor.organization_id
        if org_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        from datetime import UTC, datetime

        from zhiwei.agents.domain import TaskGraphSchema

        schema = TaskGraphSchema(**request.task_graph_schema)  # type: ignore[arg-type]
        agent = AgentDefinition(
            id=new_id(),
            name=request.name,
            description=request.description,
            version=1,
            capabilities=tuple(request.capabilities),
            task_graph_schema=schema,
            status=AgentDefinitionStatus.DRAFT,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        _agents_repo.add(agent, org_id)
        return AgentRecord(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            version=agent.version,
            status=agent.status.value,
            capabilities=list(agent.capabilities),
        )

    @router.get("/{agent_id}", response_model=AgentRecord)
    async def get_agent(
        agent_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> AgentRecord:
        org_id = actor.organization_id
        if org_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        agent = _agents_repo.get(agent_id, org_id)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
            )
        return AgentRecord(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            version=agent.version,
            status=agent.status.value,
            capabilities=list(agent.capabilities),
        )

    @router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_agent(
        agent_id: UUID,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> None:
        org_id = actor.organization_id
        if org_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization context required",
            )
        if not _agents_repo.remove(agent_id, org_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
            )

    return router
