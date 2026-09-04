"""Tenant-scoped capability repositories.

Stores capability versions and bindings per organization/workspace.
Domain layer only: no SQLAlchemy/FastAPI imports.
"""

from __future__ import annotations

from uuid import UUID

from zhiwei.capabilities.domain import (
    CapabilityBinding,
    CapabilityVersion,
    ProviderVersion,
    SkillVersion,
    ToolDefinitionVersion,
    WorkflowVersion,
)


class CapabilityRepository:
    """In-memory tenant-scoped repository for capability versions.

    Each repository instance is scoped to a single (organization_id, workspace_id).
    RLS would be enforced at the DB layer; this in-memory store simulates the
    same isolation boundary for unit tests.
    """

    def __init__(self, organization_id: UUID, workspace_id: UUID) -> None:
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self._capability_versions: dict[UUID, CapabilityVersion] = {}
        self._provider_versions: dict[UUID, ProviderVersion] = {}
        self._tool_versions: dict[UUID, ToolDefinitionVersion] = {}
        self._skill_versions: dict[UUID, SkillVersion] = {}
        self._workflow_versions: dict[UUID, WorkflowVersion] = {}

    def store_capability_version(self, version: CapabilityVersion) -> None:
        self._capability_versions[version.id] = version

    def get_capability_version(self, version_id: UUID) -> CapabilityVersion | None:
        return self._capability_versions.get(version_id)

    def store_provider_version(self, version: ProviderVersion) -> None:
        self._provider_versions[version.id] = version

    def get_provider_version(self, version_id: UUID) -> ProviderVersion | None:
        return self._provider_versions.get(version_id)

    def store_tool_version(self, version: ToolDefinitionVersion) -> None:
        self._tool_versions[version.id] = version

    def get_tool_version(self, version_id: UUID) -> ToolDefinitionVersion | None:
        return self._tool_versions.get(version_id)

    def store_skill_version(self, version: SkillVersion) -> None:
        self._skill_versions[version.id] = version

    def get_skill_version(self, version_id: UUID) -> SkillVersion | None:
        return self._skill_versions.get(version_id)

    def store_workflow_version(self, version: WorkflowVersion) -> None:
        self._workflow_versions[version.id] = version

    def get_workflow_version(self, version_id: UUID) -> WorkflowVersion | None:
        return self._workflow_versions.get(version_id)

    def remove(self, version_id: UUID) -> None:
        """Remove a capability version by ID from any store."""
        for store in (
            self._capability_versions,
            self._provider_versions,
            self._tool_versions,
            self._skill_versions,
            self._workflow_versions,
        ):
            store.pop(version_id, None)

    def list_capability_versions(self) -> list[CapabilityVersion]:
        return list(self._capability_versions.values())

    def list_provider_versions(self) -> list[ProviderVersion]:
        return list(self._provider_versions.values())

    def list_tool_versions(self) -> list[ToolDefinitionVersion]:
        return list(self._tool_versions.values())

    def list_skill_versions(self) -> list[SkillVersion]:
        return list(self._skill_versions.values())

    def list_workflow_versions(self) -> list[WorkflowVersion]:
        return list(self._workflow_versions.values())


class CapabilityBindingRepository:
    """In-memory tenant-scoped repository for capability bindings.

    Bindings associate a capability version with an agent version.
    """

    def __init__(self, organization_id: UUID, workspace_id: UUID) -> None:
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self._bindings: dict[UUID, CapabilityBinding] = {}

    def add(self, binding: CapabilityBinding) -> None:
        self._bindings[binding.id] = binding

    def get(self, binding_id: UUID) -> CapabilityBinding | None:
        return self._bindings.get(binding_id)

    def remove(self, binding_id: UUID) -> None:
        self._bindings.pop(binding_id, None)

    def list_by_agent(self, agent_definition_id: UUID) -> list[CapabilityBinding]:
        return [
            b
            for b in self._bindings.values()
            if b.agent_definition_id == agent_definition_id
        ]

    def list_by_capability(self, capability_version_id: UUID) -> list[CapabilityBinding]:
        return [
            b
            for b in self._bindings.values()
            if b.capability_version_id == capability_version_id
        ]

    def list_all(self) -> list[CapabilityBinding]:
        return list(self._bindings.values())
