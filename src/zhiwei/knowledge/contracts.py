"""S5 Source Ledger domain models.

Immutable source tracking: SourceObject/SourceVersion/Locator/SyncWatermark.
No FastAPI/Temporal/SQLAlchemy/provider SDK imports — pure Pydantic v2 frozen models.
事实源：design doc §3、S5 spec §3、ADR-003、ADR-006。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceVersionState(StrEnum):
    """Lifecycle states for a SourceVersion."""

    ACTIVE = "active"
    STALE = "stale"
    REVOKED = "revoked"


class Classification(StrEnum):
    """Data classification levels."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Locator(_FrozenModel):
    """Immutable source-native identity: where the content lives in the original system.

    locator is globally unique per source system; connector + uri combinations
    identify the same logical entity across syncs.
    """

    connector: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    version_hint: str | None = None

    @field_validator("connector", "uri")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank after stripping whitespace")
        return stripped


class ACLSnapshot(_FrozenModel):
    """ACL snapshot frozen at SourceVersion creation time.

    READ-FROZEN: the snapshot records what was true at creation; current
    visibility is re-checked at query time (ADR-006).
    """

    allowed_principals: tuple[str, ...] = Field(default_factory=tuple)
    denied_principals: tuple[str, ...] = Field(default_factory=tuple)
    allowed_groups: tuple[str, ...] = Field(default_factory=tuple)


class SourceObject(_FrozenModel):
    """An entity tracked by the Source Ledger.

    Immutable identity: id never changes. ACL and classification are
    frozen per version via SourceVersion.
    """

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    source_type: str = Field(min_length=1)
    acl: ACLSnapshot = Field(default_factory=ACLSnapshot)
    classification: Classification = Classification.PUBLIC
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceVersion(_FrozenModel):
    """A specific immutable version of a SourceObject.

    content_digest is computed from the original content and determines
    whether two versions represent the same content. Locator is the
    source-native identity for this version.
    """

    id: UUID
    source_object_id: UUID
    version_seq: int = Field(ge=1)
    locator: Locator
    content_digest: str = Field(min_length=1)
    observed_at: datetime
    valid_at: datetime
    acl: ACLSnapshot = Field(default_factory=ACLSnapshot)
    classification: Classification = Classification.PUBLIC
    state: SourceVersionState = SourceVersionState.ACTIVE
    parent_version_id: UUID | None = None
    tombstone: bool = False
    connector_version: str = Field(default="1")
    parser_version: str = Field(default="1")
    index_version: str = Field(default="1")
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1

    @field_validator("content_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("content_digest must use sha256: prefix")
        if len(value) != 71:
            raise ValueError("content_digest must be sha256:<64 hex chars>")
        return value

    @field_validator("version_seq")
    @classmethod
    def _positive_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version_seq must be >= 1")
        return value


class SyncWatermark(_FrozenModel):
    """Tracks the last successful sync point for a connector+workspace pair.

    watermark is an opaque token whose interpretation is connector-specific.
    """

    id: UUID
    connector: str = Field(min_length=1)
    organization_id: UUID
    workspace_id: UUID
    watermark: str
    last_synced_at: datetime
    last_event_id: str | None = None
    sync_count: int = Field(ge=0)
    schema_version: int = 1

    @field_validator("watermark")
    @classmethod
    def _non_empty_watermark(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("watermark must not be blank")
        return value
