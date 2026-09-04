"""Skill package loading from upstream catalog and metadata.

Loads skill packages from upstream sources, parsing metadata, assets and
allowed-tools declarations. Produces immutable SkillPackage records.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now


class SkillPackageError(RuntimeError):
    """Raised when skill package loading fails."""


class SkillAsset(BaseModel):
    """A single asset within a skill package."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    content_digest: str = ""
    size_bytes: int = 0
    url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillPackage(BaseModel):
    """An immutable loaded skill package from upstream."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(new_id()))
    name: str = Field(min_length=1)
    version: str = ""
    description: str = ""
    publisher: str = ""
    license: str | None = None
    allowed_tools: tuple[str, ...] = ()
    assets: tuple[SkillAsset, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    source_url: str = ""
    content_digest: str = ""
    loaded_at: datetime = Field(default_factory=utc_now)

    @field_validator("content_digest", mode="after")
    @classmethod
    def _compute_digest(cls, value: str, info: Any) -> str:
        if value:
            return value
        content = info.data.get("content", {})
        return digest_bytes(canonical_json(content))


class SkillPackageLoader:
    """Loads skill packages from upstream catalog metadata."""

    def load_from_metadata(
        self,
        name: str,
        metadata: dict[str, Any],
        *,
        source_url: str = "",
    ) -> SkillPackage:
        """Load a skill package from parsed metadata dict."""
        if not name:
            raise SkillPackageError("Skill package name is required")

        assets = self._parse_assets(metadata.get("assets", []))
        allowed_tools = self._parse_allowed_tools(metadata.get("allowedTools", []))
        content = metadata.get("content", {})
        content_digest = digest_bytes(canonical_json(content)) if content else ""

        return SkillPackage(
            name=name,
            version=metadata.get("version", ""),
            description=metadata.get("description", ""),
            publisher=metadata.get("publisher", ""),
            license=metadata.get("license"),
            allowed_tools=tuple(allowed_tools),
            assets=tuple(assets),
            metadata={k: v for k, v in metadata.items() if k not in {
            "assets", "allowedTools", "content", "name", "version",
            "description", "publisher", "license",
        }},
            content=content,
            source_url=source_url,
            content_digest=content_digest,
        )

    def load_from_dict(self, data: dict[str, Any]) -> SkillPackage:
        """Load a skill package from a complete package dict."""
        name = data.get("name", "")
        if not name:
            raise SkillPackageError("Skill package 'name' is required")
        return self.load_from_metadata(
            name,
            data,
            source_url=data.get("source_url", ""),
        )

    def _parse_assets(self, raw_assets: Any) -> list[SkillAsset]:
        if not isinstance(raw_assets, list):
            return []
        assets: list[SkillAsset] = []
        for item in raw_assets:
            if isinstance(item, dict) and "name" in item and "type" in item:
                assets.append(
                    SkillAsset(
                        name=item["name"],
                        asset_type=item["type"],
                        content_digest=item.get("content_digest", ""),
                        size_bytes=item.get("size_bytes", 0),
                        url=item.get("url", ""),
                        metadata=item.get("metadata", {}),
                    )
                )
        return assets

    def _parse_allowed_tools(self, raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(t) for t in raw if isinstance(t, str)]
        return []
