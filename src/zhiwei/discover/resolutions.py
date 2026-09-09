"""S8 Resolution domain types for the Discover pipeline.

Resolution 不改写原 detector output。reopen/new version/dismiss/false
positive/accepted/mitigated 等 resolution 保留完整轨迹。

事实源：specs/s8-discover-actions.md §4、§6。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ResolutionKind(StrEnum):
    """Resolution type discriminator."""

    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    FALSE_POSITIVE = "false_positive"
    MITIGATED = "mitigated"
    REOPENED = "reopened"
    SUPERSEDED = "superseded"


class Resolution(_FrozenModel):
    """Immutable resolution record for a hypothesis.

    不改写原 detector output。每种 resolution 保留完整轨迹，
    用于 audit 和 lesson candidate 提取。
    """

    id: UUID
    hypothesis_id: UUID
    case_id: UUID | None = None
    kind: ResolutionKind
    rationale: str = Field(min_length=1)
    resolved_by: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_resolution(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ResolutionChain(_FrozenModel):
    """Immutable linked chain of Resolutions.

    当同一个 hypothesis 被多次 resolution（如 dismiss → reopen → accept）时，
    保留完整轨迹。
    """

    root_resolution_id: UUID
    chain: tuple[UUID, ...] = Field(min_length=1)
    latest_resolution_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_rchain(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class LessonCandidate(_FrozenModel):
    """A lesson derived from a resolution, to be reviewed by Memory Steward.

    Resolution → lesson candidate 进入 Memory Center。
    """

    id: UUID
    resolution_id: UUID
    hypothesis_id: UUID
    summary: str = Field(min_length=1)
    category: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_lesson(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class HumanResolution(_FrozenModel):
    """Human-recorded resolution with explicit approval trail.

    用于 Case → Approval/ActionReceipt → HumanResolution → lesson candidate
    流程中的最终人工决策记录。
    """

    id: UUID
    case_id: UUID
    resolution: Resolution
    approved_by: str = Field(min_length=1)
    approval_timestamp: datetime
    notes: str = ""

    @field_validator("approval_timestamp")
    @classmethod
    def _utc_approval(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def create_resolution(
    hypothesis_id: UUID,
    kind: ResolutionKind,
    rationale: str,
    resolved_by: str,
    *,
    case_id: UUID | None = None,
    evidence_refs: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> Resolution:
    """Factory: create an immutable Resolution with generated id and timestamp."""
    return Resolution(
        id=new_id(),
        hypothesis_id=hypothesis_id,
        case_id=case_id,
        kind=kind,
        rationale=rationale,
        resolved_by=resolved_by,
        evidence_refs=evidence_refs,
        metadata=metadata or {},
        created_at=datetime.now(UTC),
    )
