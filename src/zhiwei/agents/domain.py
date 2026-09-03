"""S2 AgentDefinition and SolutionPack domain models.

不依赖 FastAPI / SQLAlchemy / provider SDK；纯 Pydantic v2 frozen models。
事实源：design doc §3.1、S2-T1 plan、ADR-005。

AgentDefinition: 代表一个 agent，含名称、描述、版本、能力和 task graph schema。
SolutionPack: 一个版本化的包，包含 agent 配置、task handlers 和依赖。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import ensure_utc


class AgentDefinitionStatus(StrEnum):
    """Agent definition lifecycle states."""

    DRAFT = "draft"
    SANDBOX = "sandbox"
    PUBLISHED = "published"


class SolutionPackStatus(StrEnum):
    """Solution pack lifecycle states."""

    DRAFT = "draft"
    SANDBOX = "sandbox"
    PUBLISHED = "published"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class TaskGraphSchema(_FrozenModel):
    """Schema defining the task graph structure for an agent.

    tasks: mapping of task name -> task configuration dict
    edges: mapping of task name -> list of dependent task names
    """

    tasks: dict[str, dict[str, Any]] = Field(min_length=1)
    edges: dict[str, list[str]] = Field(default_factory=dict)


class AgentDefinition(_FrozenModel):
    """An agent definition with name, description, version, capabilities, and task graph schema.

    Immutable once published: version field cannot be modified after publish.

    委托依赖（ADR-008 可判定化增补）：delegate_dependencies 是本 agent 经
    Delegate 原语委托的目标 agent；tool_agent_refs 是经 agent-as-tool 形式
    引用的 provider agent——两类边进入同一张委托依赖图，发布期做环检测。
    自委托必须显式声明 self_delegation_depth_cap。
    """

    id: UUID
    name: str = Field(min_length=1)
    description: str
    version: int = Field(ge=1)
    capabilities: tuple[str, ...] = Field(min_length=1)
    task_graph_schema: TaskGraphSchema
    status: AgentDefinitionStatus = AgentDefinitionStatus.DRAFT
    parent_id: UUID | None = None
    delegate_dependencies: tuple[UUID, ...] = ()
    tool_agent_refs: tuple[UUID, ...] = ()
    self_delegation_depth_cap: int | None = Field(default=None, ge=1)
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class SolutionPack(_FrozenModel):
    """A versioned package containing agent configuration, task handlers, and dependencies.

    Packs reference agent definitions via agent_definition_id.
    Content digest is computed from the canonical JSON of the content field.
    """

    id: UUID
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    agent_definition_id: UUID
    content: dict[str, Any]
    dependencies: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[UUID, ...] = ()
    status: SolutionPackStatus = SolutionPackStatus.DRAFT
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_digest(self) -> str:
        """SHA-256 digest of canonical JSON of pack content."""
        return digest_bytes(canonical_json(self.content))
