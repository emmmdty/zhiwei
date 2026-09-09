"""Skill to Tool projection.

Converts validated skill packages into ToolDefinitionVersion-compatible
structures that the capability system can execute through the Tool Gateway.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.domain import ToolDefinitionVersion
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now


class SkillProjection(BaseModel):
    """A projected tool from a skill package."""

    model_config = ConfigDict(frozen=True)

    tool_definition: ToolDefinitionVersion
    skill_name: str
    skill_version: str
    allowed_tools: tuple[str, ...] = ()
    projection_metadata: dict[str, Any] = Field(default_factory=dict)


class SkillProjectionError(RuntimeError):
    """Raised when skill-to-tool projection fails."""


class SkillProjector:
    """Projects skill packages into ToolDefinitionVersion instances."""

    def project(
        self,
        skill_name: str,
        skill_version: str,
        *,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        description: str = "",
        allowed_tools: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
        provider_version_id: Any | None = None,
    ) -> SkillProjection:
        """Project a skill into a tool definition."""
        if not skill_name:
            raise SkillProjectionError("Skill name is required for projection")

        now = utc_now()
        tool = ToolDefinitionVersion(
            id=new_id(),
            provider_version_id=provider_version_id or new_id(),
            tool_name=f"skill_{skill_name}",
            tool_type="skill",
            version=1,
            description=description,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )

        return SkillProjection(
            tool_definition=tool,
            skill_name=skill_name,
            skill_version=skill_version,
            allowed_tools=allowed_tools,
            projection_metadata=metadata or {},
        )

    def project_from_content(
        self,
        skill_name: str,
        skill_version: str,
        content: dict[str, Any],
        *,
        allowed_tools: tuple[str, ...] = (),
        provider_version_id: Any | None = None,
    ) -> SkillProjection:
        """Project a skill from its content dict."""
        input_schema = content.get("inputSchema", {})
        output_schema = content.get("outputSchema", {})
        description = content.get("description", "")
        metadata = {k: v for k, v in content.items() if k not in {"inputSchema", "outputSchema", "description"}}

        return self.project(
            skill_name,
            skill_version,
            input_schema=input_schema,
            output_schema=output_schema,
            description=description,
            allowed_tools=allowed_tools,
            metadata=metadata,
            provider_version_id=provider_version_id,
        )
