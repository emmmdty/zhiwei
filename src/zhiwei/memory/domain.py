"""S7 MemoryRecord and lifecycle domain models.

不依赖 FastAPI / SQLAlchemy / provider SDK；纯 Pydantic v2 frozen models。
事实源：S7 spec §3、DATA_MODEL.md §6、ADR-009。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.time import ensure_utc


class MemoryStatus(StrEnum):
    """MemoryRecord lifecycle states.

    candidate → confirmed → superseded / revoked / expired
    不原地覆盖；纠正创建 superseding version。
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MemoryScope(StrEnum):
    """Memory scope determines visibility and subject binding."""

    USER = "user"
    TEAM = "team"
    CASE = "case"


class MemoryType(StrEnum):
    """Memory content type determines confirmation policy and semantics."""

    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    EPISODE = "episode"
    LESSON = "lesson"


class SensitivityLevel(StrEnum):
    """Data sensitivity classification for memory records.

    影响 write policy：低风险可自动确认，敏感/derived 为 candidate。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RetentionPolicy:
    """Default retention configuration for memory records.

    candidate_ttl: candidate 超过此时间未确认自动转 expired。
    """

    __slots__ = ("candidate_ttl",)

    def __init__(self, candidate_ttl: timedelta = timedelta(days=30)) -> None:
        self.candidate_ttl = candidate_ttl

    def is_candidate_expired(self, created_at: datetime, now: datetime) -> bool:
        """Check if a candidate has exceeded its TTL."""
        elapsed = ensure_utc(now) - ensure_utc(created_at)
        return elapsed > self.candidate_ttl


_DEFAULT_RETENTION = RetentionPolicy()


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", "observed_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SourceRef(_FrozenModel):
    """Reference to an external source that produced this memory."""

    source_id: str
    source_type: str
    description: str = ""


class MemoryRecord(_FrozenModel):
    """A single memory record with full lifecycle semantics.

    字段以 DATA_MODEL.md §6 为准。status 不原地覆盖；
    纠正创建 superseding version，冲突并存直到明确解决。
    """

    id: UUID
    version: int = Field(ge=1)
    organization_id: UUID
    workspace_id: UUID
    scope: MemoryScope
    scope_subject_id: UUID
    type: MemoryType
    subject: str = Field(min_length=1)
    key: str = Field(min_length=1)
    canonical_value: str
    source_refs: tuple[SourceRef, ...] = ()
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    sensitivity: SensitivityLevel = SensitivityLevel.LOW
    status: MemoryStatus = MemoryStatus.CANDIDATE
    author_ref: UUID
    approver_ref: UUID | None = None
    conflict_refs: tuple[UUID, ...] = ()
    retention_policy: str = "default"
    allowed_profile_refs: tuple[str, ...] = ()
    acl_version: int = Field(ge=1, default=1)
    created_at: datetime
    updated_at: datetime
    superseded_by: UUID | None = None
    revoked_reason: str | None = None
    tombstone: bool = False
    schema_version: int = 1

    @field_validator("version", "schema_version", "acl_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def is_active(self) -> bool:
        """Whether this record is in an active (non-terminal) state."""
        return self.status in (MemoryStatus.CANDIDATE, MemoryStatus.CONFIRMED)

    def terminal_status(self) -> bool:
        """Whether this record is in a terminal state."""
        return self.status in (
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.EXPIRED,
        )

    def dedup_key(self) -> tuple[str, str, str, str, str, str, str]:
        """Compute the ADR-009 dedup key for candidate merging.

        (organization, workspace, scope, scope_subject, type, subject, normalized_key)
        """
        return (
            str(self.organization_id),
            str(self.workspace_id),
            self.scope.value,
            str(self.scope_subject_id),
            self.type.value,
            self.subject,
            self.key.lower().strip(),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedup_hash(self) -> str:
        """Content-addressed hash of the dedup key for efficient lookups."""
        key_tuple = self.dedup_key()
        return digest_bytes(canonical_json(list(key_tuple)))
