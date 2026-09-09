"""Skill validation: metadata integrity, allowed-tools narrowing, HTML sanitization.

Constraints from S4 spec §4:
- allowed-tools 只能收窄 (can only narrow, never widen).
- Script: freeze dependencies/OCI/SBOM.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.capabilities.skills.package import SkillPackage


class SkillValidationError(RuntimeError):
    """Raised when skill validation fails."""


class AllowedToolsViolationError(SkillValidationError):
    """Raised when allowed-tools would widen rather than narrow."""


class SkillValidationResult(BaseModel):
    """Result of validating a skill package."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    sanitized_description: str = ""
    sanitized_metadata: dict[str, Any] = Field(default_factory=dict)


class SkillValidator:
    """Validates skill packages against governance constraints."""

    _HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
    _SCRIPT_DANGEROUS_PATTERNS = (
        "eval(",
        "exec(",
        "__import__(",
        "subprocess",
        "os.system",
        "open(",
    )

    def validate(
        self,
        package: SkillPackage,
        *,
        parent_allowed_tools: tuple[str, ...] | None = None,
    ) -> SkillValidationResult:
        """Validate a skill package and return results with sanitized content."""
        errors: list[str] = []
        warnings: list[str] = []

        if not package.name:
            errors.append("Skill package name is empty")

        if not package.version:
            warnings.append("Skill package version is empty")

        if parent_allowed_tools is not None:
            self._validate_allowed_tools_narrowing(
                package.allowed_tools, parent_allowed_tools, errors
            )

        sanitized_desc = self._sanitize_html(package.description)
        if sanitized_desc != package.description:
            warnings.append("Description contained HTML tags that were sanitized")

        sanitized_meta = self._sanitize_metadata_html(package.metadata)

        return SkillValidationResult(
            valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
            sanitized_description=sanitized_desc,
            sanitized_metadata=sanitized_meta,
        )

    def _validate_allowed_tools_narrowing(
        self,
        child_tools: tuple[str, ...],
        parent_tools: tuple[str, ...],
        errors: list[str],
    ) -> None:
        """Ensure child allowed-tools is a subset of parent (narrowing only)."""
        child_set = set(child_tools)
        parent_set = set(parent_tools)

        if not child_set:
            return

        if not parent_set:
            errors.append(
                "Cannot narrow tools: parent has no allowed-tools definition"
            )
            return

        widening = child_set - parent_set
        if widening:
            errors.append(
                f"allowed-tools can only narrow; these tools are not in parent: "
                f"{sorted(widening)}"
            )

    def _sanitize_html(self, text: str) -> str:
        """Remove HTML tags from text content."""
        return self._HTML_TAG_PATTERN.sub("", text).strip()

    def _sanitize_metadata_html(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Sanitize HTML in string metadata values."""
        sanitized: dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, str):
                sanitized[key] = self._sanitize_html(value)
            else:
                sanitized[key] = value
        return sanitized

    def validate_script_safety(self, script_content: str) -> SkillValidationResult:
        """Validate that a script does not contain dangerous patterns."""
        errors: list[str] = []
        warnings: list[str] = []

        for pattern in self._SCRIPT_DANGEROUS_PATTERNS:
            if pattern in script_content:
                errors.append(f"Script contains dangerous pattern: {pattern}")

        if len(script_content) > 1_000_000:
            warnings.append("Script content is very large (>1MB)")

        return SkillValidationResult(
            valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
