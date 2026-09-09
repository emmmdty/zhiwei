"""S2-T1 RED: Version lifecycle management for AgentDefinition and SolutionPack."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.agents.domain import (
    AgentDefinition,
    AgentDefinitionStatus,
    SolutionPack,
    SolutionPackStatus,
    TaskGraphSchema,
)
from zhiwei.agents.versions import (
    AgentVersionManager,
    InvalidPackReferenceError,
    InvalidParentReferenceError,
    PackVersionManager,
    VersionStateError,
)
from zhiwei.contracts.identifiers import new_id


def _make_task_graph_schema() -> TaskGraphSchema:
    return TaskGraphSchema(
        tasks={"step1": {"type": "prompt", "template": "Hello"}},
        edges={"step1": []},
    )


def _make_agent_definition(**overrides: object) -> AgentDefinition:
    defaults = {
        "id": new_id(),
        "name": "test-agent",
        "description": "A test agent",
        "version": 1,
        "capabilities": ("prompt",),
        "task_graph_schema": _make_task_graph_schema(),
        "status": AgentDefinitionStatus.DRAFT,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return AgentDefinition(**defaults)


def _make_solution_pack(**overrides: object) -> SolutionPack:
    defaults = {
        "id": new_id(),
        "name": "test-pack",
        "version": 1,
        "agent_definition_id": new_id(),
        "content": {"handlers": {"step1": "echo"}},
        "dependencies": {},
        "status": SolutionPackStatus.DRAFT,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return SolutionPack(**defaults)


class TestAgentVersionManager:
    def test_create_draft(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        assert agent.status == AgentDefinitionStatus.DRAFT
        assert agent.version == 1
        assert agent.name == "my-agent"

    def test_promote_draft_to_sandbox(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        sandbox = mgr.promote_to_sandbox(agent.id)
        assert sandbox.status == AgentDefinitionStatus.SANDBOX
        assert sandbox.id == agent.id

    def test_promote_sandbox_to_published(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        mgr.promote_to_sandbox(agent.id)
        published = mgr.promote_to_published(agent.id)
        assert published.status == AgentDefinitionStatus.PUBLISHED

    def test_cannot_promote_nonexistent_agent(self) -> None:
        mgr = AgentVersionManager()
        with pytest.raises(InvalidParentReferenceError):
            mgr.promote_to_sandbox(new_id())

    def test_cannot_promote_published_back_to_sandbox(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        mgr.promote_to_sandbox(agent.id)
        mgr.promote_to_published(agent.id)
        with pytest.raises(VersionStateError):
            mgr.promote_to_sandbox(agent.id)

    def test_cannot_promote_draft_directly_to_published(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        with pytest.raises(VersionStateError):
            mgr.promote_to_published(agent.id)

    def test_immutability_after_publish(self) -> None:
        mgr = AgentVersionManager()
        agent = mgr.create_draft(name="my-agent", description="desc", capabilities=("prompt",))
        mgr.promote_to_sandbox(agent.id)
        published = mgr.promote_to_published(agent.id)
        with pytest.raises(ValidationError):
            published.version = 2  # type: ignore[misc]


class TestPackVersionManager:
    def test_create_draft(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        assert pack.status == SolutionPackStatus.DRAFT
        assert pack.version == 1

    def test_promote_draft_to_sandbox(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        sandbox = mgr.promote_to_sandbox(pack.id)
        assert sandbox.status == SolutionPackStatus.SANDBOX

    def test_promote_sandbox_to_published(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        mgr.promote_to_sandbox(pack.id)
        published = mgr.promote_to_published(pack.id)
        assert published.status == SolutionPackStatus.PUBLISHED

    def test_cannot_promote_nonexistent_pack(self) -> None:
        mgr = PackVersionManager()
        with pytest.raises(InvalidParentReferenceError):
            mgr.promote_to_sandbox(new_id())

    def test_cannot_promote_published_back_to_sandbox(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        mgr.promote_to_sandbox(pack.id)
        mgr.promote_to_published(pack.id)
        with pytest.raises(VersionStateError):
            mgr.promote_to_sandbox(pack.id)

    def test_cannot_promote_draft_directly_to_published(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        with pytest.raises(VersionStateError):
            mgr.promote_to_published(pack.id)

    def test_invalid_pack_reference_rejected(self) -> None:
        mgr = PackVersionManager()
        with pytest.raises(InvalidPackReferenceError):
            mgr.create_draft(
                name="my-pack",
                agent_definition_id=new_id(),
                content={"handlers": {}},
                depends_on=(new_id(),),
            )

    def test_valid_pack_reference_accepted(self) -> None:
        mgr = PackVersionManager()
        dep_pack = mgr.create_draft(
            name="dep-pack",
            agent_definition_id=new_id(),
            content={"handlers": {}},
        )
        mgr.promote_to_sandbox(dep_pack.id)
        mgr.promote_to_published(dep_pack.id)

        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {}},
            depends_on=(dep_pack.id,),
        )
        assert pack.depends_on == (dep_pack.id,)

    def test_immutability_after_publish(self) -> None:
        mgr = PackVersionManager()
        pack = mgr.create_draft(
            name="my-pack",
            agent_definition_id=new_id(),
            content={"handlers": {"step1": "echo"}},
        )
        mgr.promote_to_sandbox(pack.id)
        published = mgr.promote_to_published(pack.id)
        with pytest.raises(ValidationError):
            published.version = 2  # type: ignore[misc]
