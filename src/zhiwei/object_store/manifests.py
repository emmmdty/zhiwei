"""Typed artifact manifest commands and verification results."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ArtifactVerificationError(RuntimeError):
    """Raised when stored bytes do not reproduce their manifest digest and size."""


class ArtifactManifestCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_resource_type: str = Field(min_length=1, max_length=64)
    owner_resource_id: UUID
    content_digest: str
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    artifact_schema_id: str = Field(pattern=r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*")
    artifact_schema_version: int = Field(gt=0)
    classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
    retention: dict[str, Any]
    encryption_key_ref: str | None = Field(default=None, max_length=255)

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("content_digest must be lowercase SHA-256")
        return value


class CommittedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_id: UUID
    object_key: str
    content_digest: str
    size_bytes: int
