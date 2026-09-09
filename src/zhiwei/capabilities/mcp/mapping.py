"""Map MCP tools/resources/prompts to domain ToolDefinition/ResourceDefinition.

Maps MCP server capabilities into the ZhiWei capability domain models
(ToolDefinitionVersion, ResourceDefinition) so they can go through the
admission/version/binding lifecycle.

S4 spec §4: tools 映射 ToolDefinition, resources 映射 ResourceDefinition/
SourceObservationProvider port; S5 才在 Knowledge policy 下创建 DataSource/
SourceVersion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from zhiwei.capabilities.domain import (
    CapabilityStatus,
    RiskLevel,
    ToolDefinitionVersion,
)
from zhiwei.contracts.identifiers import new_id


class MappingError(Exception):
    """Error when mapping MCP types to domain types."""


class McpToolNameConflictError(MappingError):
    """同一命名域内工具名已被其他 provider version 占用（跨 server 影子化，F-R9-09）。"""


class McpToolNameRegistry:
    """(命名域, tool_name) 唯一性裁决表（F-R9-09）。

    registry 实例由调用方按命名域（如 workspace）持有：同一域内两个 server 的
    同名工具在注册期冲突即拒绝，而不是静默遮蔽。同 provider version 重复注册
    幂等（重放/重试安全）。
    """

    def __init__(self) -> None:
        self._owner: dict[str, UUID] = {}

    def register(self, tool_name: str, provider_version_id: UUID) -> None:
        owner = self._owner.get(tool_name)
        if owner is not None and owner != provider_version_id:
            raise McpToolNameConflictError(
                f"tool name '{tool_name}' is already mapped by provider "
                f"version {owner}; shadowing across servers is forbidden"
            )
        self._owner[tool_name] = provider_version_id


class ResourceDefinition:
    """Domain resource definition mapped from MCP resource.

    S4 spec §4: resources 映射 ResourceDefinition/SourceObservationProvider port。
    DataSource/SourceVersion creation is deferred to S5.
    """

    def __init__(
        self,
        id: UUID,
        provider_version_id: UUID,
        uri: str,
        name: str,
        description: str = "",
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: CapabilityStatus = CapabilityStatus.DISCOVERED,
    ) -> None:
        self.id = id
        self.provider_version_id = provider_version_id
        self.uri = uri
        self.name = name
        self.description = description
        self.mime_type = mime_type
        self.metadata = metadata or {}
        self.status = status
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class PromptDefinition:
    """Domain prompt definition mapped from MCP prompt."""

    def __init__(
        self,
        id: UUID,
        provider_version_id: UUID,
        name: str,
        description: str = "",
        arguments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.provider_version_id = provider_version_id
        self.name = name
        self.description = description
        self.arguments = arguments or []
        self.metadata = metadata or {}
        self.created_at = datetime.now(UTC)


def map_mcp_tool_to_tool_definition(
    mcp_tool: dict[str, Any],
    provider_version_id: UUID,
    *,
    risk_level: RiskLevel = RiskLevel.LOW,
    classification: str = "PUBLIC",
) -> ToolDefinitionVersion:
    """Map an MCP tool definition to a ZhiWei ToolDefinitionVersion.

    MCP tool schema:
    {
        "name": "tool_name",
        "description": "...",
        "inputSchema": { "type": "object", "properties": {...} }
    }
    """
    name = mcp_tool.get("name")
    if not name:
        raise MappingError("MCP tool must have a 'name' field")

    description = mcp_tool.get("description", "")
    input_schema = mcp_tool.get("inputSchema", {})
    output_schema = mcp_tool.get("outputSchema", {})

    now = datetime.now(UTC)
    return ToolDefinitionVersion(
        id=new_id(),
        provider_version_id=provider_version_id,
        tool_name=name,
        tool_type="mcp_tool",
        version=1,
        description=description,
        status=CapabilityStatus.DISCOVERED,
        classification=classification,
        input_schema=input_schema,
        output_schema=output_schema,
        metadata={"source": "mcp", "mcp_name": name},
        risk_level=risk_level,
        created_at=now,
        updated_at=now,
    )


def map_mcp_resource_to_resource_definition(
    mcp_resource: dict[str, Any],
    provider_version_id: UUID,
) -> ResourceDefinition:
    """Map an MCP resource to a ZhiWei ResourceDefinition.

    MCP resource schema:
    {
        "uri": "file:///path/to/resource",
        "name": "resource_name",
        "description": "...",
        "mimeType": "text/plain"
    }
    """
    uri = mcp_resource.get("uri")
    if not uri:
        raise MappingError("MCP resource must have a 'uri' field")

    name = mcp_resource.get("name", "")

    return ResourceDefinition(
        id=new_id(),
        provider_version_id=provider_version_id,
        uri=uri,
        name=name,
        description=mcp_resource.get("description", ""),
        mime_type=mcp_resource.get("mimeType"),
        metadata={"source": "mcp"},
    )


def map_mcp_prompt_to_prompt_definition(
    mcp_prompt: dict[str, Any],
    provider_version_id: UUID,
) -> PromptDefinition:
    """Map an MCP prompt to a ZhiWei PromptDefinition.

    MCP prompt schema:
    {
        "name": "prompt_name",
        "description": "...",
        "arguments": [{"name": "...", "description": "...", "required": false}]
    }
    """
    name = mcp_prompt.get("name")
    if not name:
        raise MappingError("MCP prompt must have a 'name' field")

    return PromptDefinition(
        id=new_id(),
        provider_version_id=provider_version_id,
        name=name,
        description=mcp_prompt.get("description", ""),
        arguments=mcp_prompt.get("arguments", []),
        metadata={"source": "mcp"},
    )


def map_mcp_tools_batch(
    mcp_tools: list[dict[str, Any]],
    provider_version_id: UUID,
    *,
    risk_level: RiskLevel = RiskLevel.LOW,
    classification: str = "PUBLIC",
    name_registry: McpToolNameRegistry | None = None,
) -> list[ToolDefinitionVersion]:
    """Map a batch of MCP tools to ToolDefinitionVersions.

    传入 name_registry 时逐名注册做唯一性裁决（F-R9-09）；冲突即中断并抛出。
    """
    mapped: list[ToolDefinitionVersion] = []
    for tool in mcp_tools:
        definition = map_mcp_tool_to_tool_definition(
            tool,
            provider_version_id,
            risk_level=risk_level,
            classification=classification,
        )
        if name_registry is not None:
            name_registry.register(definition.tool_name, provider_version_id)
        mapped.append(definition)
    return mapped


def map_mcp_resources_batch(
    mcp_resources: list[dict[str, Any]],
    provider_version_id: UUID,
) -> list[ResourceDefinition]:
    """Map a batch of MCP resources to ResourceDefinitions."""
    return [
        map_mcp_resource_to_resource_definition(resource, provider_version_id)
        for resource in mcp_resources
    ]
