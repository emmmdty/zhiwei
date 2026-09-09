"""S4 Capability domain types.

不依赖 FastAPI / SQLAlchemy / provider SDK；纯 Pydantic v2 frozen models。
事实源：S4 spec §3（Resource and lifecycle）、S4 plan Task 1。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import ensure_utc


class CapabilityStatus(StrEnum):
    """Capability lifecycle states.

    discovered → quarantined → inspected → tested → approved → published
    → deprecated / suspended / revoked
    """

    DISCOVERED = "discovered"
    QUARANTINED = "quarantined"
    INSPECTED = "inspected"
    TESTED = "tested"
    APPROVED = "approved"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class RiskLevel(StrEnum):
    """Capability risk classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProviderVersion(_FrozenModel):
    """Immutable version of a capability provider (MCP, OpenAPI, SDK, etc)."""

    id: UUID
    provider_id: UUID
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    classification: str = "PUBLIC"
    source_url: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_digest(self) -> str:
        """SHA-256 digest of canonical JSON of provider content."""
        return digest_bytes(canonical_json(self.content))


class ToolDefinitionVersion(_FrozenModel):
    """Immutable version of a tool definition (MCP tool, OpenAPI operation, etc)."""

    id: UUID
    provider_version_id: UUID
    tool_name: str = Field(min_length=1)
    tool_type: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    classification: str = "PUBLIC"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_digest(self) -> str:
        """SHA-256 digest of canonical JSON of tool definition content."""
        return digest_bytes(
            canonical_json(
                {
                    "input_schema": self.input_schema,
                    "output_schema": self.output_schema,
                }
            )
        )


class SkillVersion(_FrozenModel):
    """Immutable version of an agent skill."""

    id: UUID
    skill_id: UUID
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    classification: str = "PUBLIC"
    content: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_digest(self) -> str:
        """SHA-256 digest of canonical JSON of skill content."""
        return digest_bytes(canonical_json(self.content))


class WorkflowVersion(_FrozenModel):
    """Immutable version of a workflow definition."""

    id: UUID
    workflow_id: UUID
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    classification: str = "PUBLIC"
    content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_digest(self) -> str:
        """SHA-256 digest of canonical JSON of workflow content."""
        return digest_bytes(canonical_json(self.content))


class CapabilityVersion(_FrozenModel):
    """Generic capability version wrapper used by the version manager.

    Wraps any capability resource (Provider, Tool, Skill, Workflow) and tracks
    its lifecycle state independently of the agent version it may be bound to.
    """

    id: UUID
    capability_type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    risk_level: RiskLevel = RiskLevel.LOW
    content_digest: str = ""
    test_digest: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class CapabilityBinding(_FrozenModel):
    """Binding between a capability version and an agent version.

    Upstream updates create candidate CapabilityVersions; binding is explicit
    and does not change on upstream update (S4 spec §3).
    """

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    agent_definition_id: UUID
    agent_version_id: UUID
    capability_version_id: UUID
    status: str = "active"
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value
