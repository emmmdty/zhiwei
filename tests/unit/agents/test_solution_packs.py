"""S2-T1 RED: SolutionPack loading and validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhiwei.agents.domain import (
    AgentDefinition,
    AgentDefinitionStatus,
    SolutionPack,
    SolutionPackStatus,
)
from zhiwei.agents.solution_packs import (
    PackLoader,
    PackValidationError,
)
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id


def _make_agent_definition(**overrides: object) -> AgentDefinition:
    defaults = {
        "id": new_id(),
        "name": "test-agent",
        "description": "A test agent",
        "version": 1,
        "capabilities": ("prompt",),
        "task_graph_schema": {"tasks": {"s1": {"type": "prompt"}}, "edges": {"s1": []}},
        "status": AgentDefinitionStatus.PUBLISHED,
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


class TestPackLoader:
    def test_load_pack_from_dict(self) -> None:
        loader = PackLoader()
        agent = _make_agent_definition()
        loader.register_agent(agent)

        pack = _make_solution_pack(agent_definition_id=agent.id)
        loaded = loader.load(pack.model_dump(mode="json"))
        assert loaded.name == pack.name
        assert loaded.agent_definition_id == agent.id

    def test_rejects_invalid_agent_reference(self) -> None:
        loader = PackLoader()
        pack = _make_solution_pack()
        with pytest.raises(PackValidationError, match="Agent definition"):
            loader.load(pack.model_dump(mode="json"))

    def test_rejects_invalid_dependency_reference(self) -> None:
        loader = PackLoader()
        agent = _make_agent_definition()
        loader.register_agent(agent)

        pack = _make_solution_pack(
            agent_definition_id=agent.id,
            depends_on=(new_id(),),
        )
        with pytest.raises(PackValidationError, match="Dependency pack"):
            loader.load(pack.model_dump(mode="json"))

    def test_content_digest_is_computed_correctly(self) -> None:
        loader = PackLoader()
        agent = _make_agent_definition()
        loader.register_agent(agent)

        pack = _make_solution_pack(agent_definition_id=agent.id)
        loaded = loader.load(pack.model_dump(mode="json"))
        expected_digest = digest_bytes(canonical_json(pack.content))
        assert loaded.content_digest == expected_digest

    def test_valid_dependency_chain_accepted(self) -> None:
        loader = PackLoader()
        agent = _make_agent_definition()
        loader.register_agent(agent)

        dep_pack = _make_solution_pack(
            name="dep",
            agent_definition_id=agent.id,
            status=SolutionPackStatus.PUBLISHED,
        )
        loader.register_pack(dep_pack)

        main_pack = _make_solution_pack(
            name="main",
            agent_definition_id=agent.id,
            depends_on=(dep_pack.id,),
        )
        loaded = loader.load(main_pack.model_dump(mode="json"))
        assert loaded.depends_on == (dep_pack.id,)

    def test_rejects_missing_required_fields(self) -> None:
        loader = PackLoader()
        with pytest.raises(PackValidationError):
            loader.load({"name": "incomplete"})
