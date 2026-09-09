"""S6 Case domain types.

Cases are user-created or attached groupings of answers and evidence.
They do not copy transcript; they reference answers by id.

事实源：S6 spec §4。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc


class CaseStatus(StrEnum):
    """Case lifecycle states (spec s6 §4.1).

    Frozen machine: created → active → triaged → resolved → archived.
    OPEN is retained for backward compatibility with pre-S6 persisted cases;
    it is not part of the frozen machine and must not be produced by new code.
    """

    CREATED = "created"
    ACTIVE = "active"
    TRIAGED = "triaged"
    OPEN = "open"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Case(_FrozenModel):
    """A user-created case grouping answers and evidence references.

    Cases reference answers and evidence by id; they do not duplicate
    transcript content. Users can create a case from an answer or attach
    selected evidence to an existing case.
    """

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    title: str = Field(min_length=1)
    description: str = ""
    status: CaseStatus = CaseStatus.OPEN
    answer_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    evidence_bundle_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value
