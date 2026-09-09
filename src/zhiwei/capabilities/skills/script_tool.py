"""Script-based tool with frozen dependencies, OCI digest and SBOM.

Constraints from S4 spec §4:
- Script 冻结依赖/OCI/SBOM 并注册 Tool.
- Executable Skill becomes a versioned Tool, never a host subprocess.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.capabilities.domain import ToolDefinitionVersion
from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now


class ScriptToolError(RuntimeError):
    """Raised when script tool validation fails."""


class FrozenDependency(BaseModel):
    """A pinned dependency with digest."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = ""
    source: str = ""


class SBOMEntry(BaseModel):
    """A Software Bill of Materials entry."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = ""
    license: str | None = None
    digest: str = ""
    source_url: str = ""


class OCIImage(BaseModel):
    """Immutable OCI container image reference."""

    model_config = ConfigDict(frozen=True)

    registry: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    tag: str = ""

    @field_validator("digest")
    @classmethod
    def _validate_digest_format(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("OCI digest must use sha256: prefix")
        return value


class ScriptTool(BaseModel):
    """An immutable script-based tool with frozen supply chain."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(new_id()))
    name: str = Field(min_length=1)
    description: str = ""
    script_content: str = ""
    script_digest: str = ""
    dependencies: tuple[FrozenDependency, ...] = ()
    sbom: tuple[SBOMEntry, ...] = ()
    oci_image: OCIImage | None = None
    entry_point: str = "main"
    runtime: str = "python3.11"
    timeout_seconds: int = 300
    network_access: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("script_digest", mode="after")
    @classmethod
    def _compute_script_digest(cls, value: str, info: Any) -> str:
        if value:
            return value
        content = info.data.get("script_content", "")
        return digest_bytes(canonical_json(content)) if content else ""


class ScriptToolBuilder:
    """Builds ScriptTool instances with frozen supply chain."""

    def build(
        self,
        name: str,
        script_content: str,
        *,
        description: str = "",
        dependencies: list[dict[str, Any]] | None = None,
        sbom: list[dict[str, Any]] | None = None,
        oci_image: dict[str, Any] | None = None,
        entry_point: str = "main",
        runtime: str = "python3.11",
        timeout_seconds: int = 300,
        network_access: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ScriptTool:
        """Build a script tool with frozen dependencies."""
        if not name:
            raise ScriptToolError("Script tool name is required")
        if not script_content:
            raise ScriptToolError("Script content is required")

        frozen_deps = tuple(
            FrozenDependency(
                name=d["name"],
                version=d["version"],
                digest=d.get("digest", ""),
                source=d.get("source", ""),
            )
            for d in (dependencies or [])
            if isinstance(d, dict) and "name" in d and "version" in d
        )

        frozen_sbom = tuple(
            SBOMEntry(
                name=s["name"],
                version=s.get("version", ""),
                license=s.get("license"),
                digest=s.get("digest", ""),
                source_url=s.get("source_url", ""),
            )
            for s in (sbom or [])
            if isinstance(s, dict) and "name" in s
        )

        oci = None
        if oci_image and isinstance(oci_image, dict):
            oci = OCIImage(
                registry=oci_image.get("registry", ""),
                repository=oci_image.get("repository", ""),
                digest=oci_image.get("digest", "sha256:" + "0" * 64),
                tag=oci_image.get("tag", ""),
            )

        script_digest = digest_bytes(canonical_json(script_content))

        return ScriptTool(
            name=name,
            description=description,
            script_content=script_content,
            script_digest=script_digest,
            dependencies=frozen_deps,
            sbom=frozen_sbom,
            oci_image=oci,
            entry_point=entry_point,
            runtime=runtime,
            timeout_seconds=timeout_seconds,
            network_access=network_access,
            metadata=metadata or {},
        )

    def to_tool_definition(self, script_tool: ScriptTool) -> ToolDefinitionVersion:
        """Convert a ScriptTool to a ToolDefinitionVersion for the gateway."""
        now = utc_now()
        metadata = {
            "runtime": script_tool.runtime,
            "entry_point": script_tool.entry_point,
            "timeout_seconds": script_tool.timeout_seconds,
            "network_access": script_tool.network_access,
            "script_digest": script_tool.script_digest,
            "dependency_count": len(script_tool.dependencies),
            "sbom_count": len(script_tool.sbom),
        }
        if script_tool.oci_image:
            metadata["oci_digest"] = script_tool.oci_image.digest

        return ToolDefinitionVersion(
            id=new_id(),
            provider_version_id=new_id(),
            tool_name=script_tool.name,
            tool_type="script",
            version=1,
            description=script_tool.description,
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
