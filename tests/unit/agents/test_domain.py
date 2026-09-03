"""S2-T1 RED: AgentDefinition and SolutionPack domain models."""

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
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id


def _make_task_graph_schema() -> TaskGraphSchema:
    return TaskGraphSchema(
        tasks={"step1": {"type": "prompt", "template": "Hello {{name}}"}},
        edges={"step1": []},
    )


def _make_agent_definition(**overrides: object) -> AgentDefinition:
    defaults = {
        "id": new_id(),
        "name": "test-agent",
        "description": "A test agent",
        "version": 1,
        "capabilities": ("prompt", "tool_use"),
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


# ---- AgentDefinition tests ----

class TestAgentDefinition:
    def test_creation_with_valid_fields(self) -> None:
        agent = _make_agent_definition()
        assert agent.name == "test-agent"
        assert agent.version == 1
        assert agent.status == AgentDefinitionStatus.DRAFT

    def test_frozen_model_rejects_field_mutation(self) -> None:
        agent = _make_agent_definition()
        with pytest.raises(ValidationError):
            agent.name = "changed"  # type: ignore[misc]

    def test_immutability_of_published_version(self) -> None:
        agent = _make_agent_definition(status=AgentDefinitionStatus.PUBLISHED)
        with pytest.raises(ValidationError):
            agent.version = 2  # type: ignore[misc]

    def test_task_graph_schema_is_frozen(self) -> None:
        schema = _make_task_graph_schema()
        with pytest.raises(ValidationError):
            schema.tasks = {}  # type: ignore[misc]

    def test_status_transitions_are_valid(self) -> None:
        draft = _make_agent_definition(status=AgentDefinitionStatus.DRAFT)
        assert draft.status == AgentDefinitionStatus.DRAFT
        sandbox = draft.model_copy(update={"status": AgentDefinitionStatus.SANDBOX})
        assert sandbox.status == AgentDefinitionStatus.SANDBOX
        published = sandbox.model_copy(update={"status": AgentDefinitionStatus.PUBLISHED})
        assert published.status == AgentDefinitionStatus.PUBLISHED

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            _make_agent_definition(name="")

    def test_rejects_negative_version(self) -> None:
        with pytest.raises(ValidationError):
            _make_agent_definition(version=0)

    def test_capabilities_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            _make_agent_definition(capabilities=())


# ---- SolutionPack tests ----

class TestSolutionPack:
    def test_creation_with_valid_fields(self) -> None:
        pack = _make_solution_pack()
        assert pack.name == "test-pack"
        assert pack.version == 1
        assert pack.status == SolutionPackStatus.DRAFT

    def test_content_digest_is_computed(self) -> None:
        pack = _make_solution_pack()
        expected = digest_bytes(canonical_json(pack.content))
        assert pack.content_digest == expected

    def test_content_digest_is_deterministic(self) -> None:
        pack_a = _make_solution_pack()
        pack_b = _make_solution_pack()
        assert pack_a.content_digest == pack_b.content_digest

    def test_content_digest_changes_with_content(self) -> None:
        pack = _make_solution_pack()
        pack_changed = pack.model_copy(
            update={"content": {"handlers": {"step1": "different"}}}
        )
        assert pack.content_digest != pack_changed.content_digest

    def test_frozen_model_rejects_field_mutation(self) -> None:
        pack = _make_solution_pack()
        with pytest.raises(ValidationError):
            pack.name = "changed"  # type: ignore[misc]

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            _make_solution_pack(name="")

    def test_rejects_negative_version(self) -> None:
        with pytest.raises(ValidationError):
            _make_solution_pack(version=0)

    def test_status_transitions_are_valid(self) -> None:
        draft = _make_solution_pack(status=SolutionPackStatus.DRAFT)
        sandbox = draft.model_copy(update={"status": SolutionPackStatus.SANDBOX})
        assert sandbox.status == SolutionPackStatus.SANDBOX
        published = sandbox.model_copy(update={"status": SolutionPackStatus.PUBLISHED})
        assert published.status == SolutionPackStatus.PUBLISHED
