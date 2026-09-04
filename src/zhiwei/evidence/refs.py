"""S6 EvidenceRef tagged union.

Eight variants: QueryReplay, CellRef, DocRef, CodeRef, GitHubRef, ApiRef,
AgentRef, PatternRef. Each carries reproducibility_level per ADR-003.

事实源：S6 spec §3、ADR-003、ADR-006。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.contracts.time import ensure_utc
from zhiwei.evidence.canonical_values import (
    CopyFrozenMetadata,
    ReproducibilityLevel,
)
from zhiwei.evidence.errors import (
    ClaimLevelViolationError,
    CopyFrozenBindingError,
    EvidenceRefValidationError,
)


class EvidenceRefType(StrEnum):
    """Tag discriminator for EvidenceRef variants."""

    QUERY_REPLAY = "QueryReplay"
    CELL_REF = "CellRef"
    DOC_REF = "DocRef"
    CODE_REF = "CodeRef"
    GITHUB_REF = "GitHubRef"
    API_REF = "ApiRef"
    AGENT_REF = "AgentRef"
    PATTERN_REF = "PatternRef"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _BaseEvidenceRef(_FrozenModel):
    """Fields common to all EvidenceRef variants."""

    ref_id: UUID
    ref_type: EvidenceRefType
    reproducibility_level: ReproducibilityLevel
    source_id: UUID
    snapshot_digest: str | None = None
    classification: str = "PUBLIC"
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("snapshot_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("sha256:"):
            raise ValueError("snapshot_digest must use sha256: prefix")
        return value


def _check_copy_frozen_binding(
    level: ReproducibilityLevel,
    copy_frozen: CopyFrozenMetadata | None,
    ref_name: str,
) -> None:
    """Validate copy_frozen metadata binding for a given reproducibility level."""
    if level == ReproducibilityLevel.COPY_FROZEN and copy_frozen is None:
        raise CopyFrozenBindingError(
            f"{ref_name} with copy_frozen level requires copy_frozen metadata"
        )
    if level == ReproducibilityLevel.REFERENCE_ONLY and copy_frozen is not None:
        raise EvidenceRefValidationError(
            f"reference_only {ref_name} must not carry copy_frozen metadata"
        )


class QueryReplayRef(_BaseEvidenceRef):
    """Query can be re-executed on original snapshot for byte-identical result.

    Binds sql, params, and snapshot info. replayable level.
    """

    ref_type: EvidenceRefType = EvidenceRefType.QUERY_REPLAY
    sql: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _level_requires_replayable(self) -> QueryReplayRef:
        if self.reproducibility_level != ReproducibilityLevel.REPLAYABLE:
            raise EvidenceRefValidationError(
                "QueryReplayRef requires reproducibility_level=replayable"
            )
        return self


class CellRef(_BaseEvidenceRef):
    """Reference to a specific cell in a structured data source.

    Binds table, column, row locator. Supports replayable or copy_frozen.
    """

    ref_type: EvidenceRefType = EvidenceRefType.CELL_REF
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)
    row_locator: dict[str, Any] = Field(default_factory=dict)
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> CellRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "CellRef"
        )
        return self


class DocRef(_BaseEvidenceRef):
    """Reference to a document (file, page, section).

    Supports any reproducibility level. copy_frozen binds document hash.
    """

    ref_type: EvidenceRefType = EvidenceRefType.DOC_REF
    document_uri: str = Field(min_length=1)
    section_path: str | None = None
    content_hash: str | None = None
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> DocRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "DocRef"
        )
        return self


class CodeRef(_BaseEvidenceRef):
    """Reference to a code location (file, line span, digest).

    Supports any reproducibility level.
    """

    ref_type: EvidenceRefType = EvidenceRefType.CODE_REF
    file_path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    code_digest: str = Field(min_length=1)
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> CodeRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "CodeRef"
        )
        return self

    @field_validator("line_end")
    @classmethod
    def _line_end_gte_start(cls, value: int, info: Any) -> int:
        start = info.data.get("line_start")
        if start is not None and value < start:
            raise ValueError("line_end must be >= line_start")
        return value


class GitHubRef(_BaseEvidenceRef):
    """Reference to a GitHub resource (commit, PR, file, issue).

    Supports any reproducibility level.
    """

    ref_type: EvidenceRefType = EvidenceRefType.GITHUB_REF
    repository: str = Field(min_length=1)
    commit_sha: str | None = None
    path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    pr_number: int | None = Field(default=None, ge=1)
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> GitHubRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "GitHubRef"
        )
        return self


class ApiRef(_BaseEvidenceRef):
    """Reference to an external API response.

    Supports any reproducibility level.
    """

    ref_type: EvidenceRefType = EvidenceRefType.API_REF
    endpoint: str = Field(min_length=1)
    method: str = Field(default="GET", min_length=1)
    response_hash: str | None = None
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> ApiRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "ApiRef"
        )
        return self


class AgentRef(_BaseEvidenceRef):
    """Reference to another agent's output or run.

    Supports any reproducibility level.
    """

    ref_type: EvidenceRefType = EvidenceRefType.AGENT_REF
    agent_id: UUID
    run_id: UUID | None = None
    output_digest: str | None = None
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> AgentRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "AgentRef"
        )
        return self


class PatternRef(_BaseEvidenceRef):
    """Reference to a known pattern or heuristic.

    Typically reference_only. Pattern is identified by name and version.
    """

    ref_type: EvidenceRefType = EvidenceRefType.PATTERN_REF
    pattern_name: str = Field(min_length=1)
    pattern_version: str = Field(default="1")
    copy_frozen: CopyFrozenMetadata | None = None

    @model_validator(mode="after")
    def _level_and_binding_consistency(self) -> PatternRef:
        _check_copy_frozen_binding(
            self.reproducibility_level, self.copy_frozen, "PatternRef"
        )
        return self


# Union type for all EvidenceRef variants
EvidenceRef = (
    QueryReplayRef
    | CellRef
    | DocRef
    | CodeRef
    | GitHubRef
    | ApiRef
    | AgentRef
    | PatternRef
)


def validate_evidence_ref_level_supports_claim(
    ref: EvidenceRef,
    claim_type: str,
) -> None:
    """Validate that an EvidenceRef's reproducibility level supports the given claim type.

    Fact/Quote require replayable or copy_frozen.
    Inference/Recommendation accept any level.
    Raises ClaimLevelViolationError on violation.
    """
    if (
        claim_type in ("Fact", "Quote")
        and ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY
    ):
        raise ClaimLevelViolationError(
            f"{claim_type} claim requires replayable or copy_frozen evidence; "
            f"got reference_only on {ref.ref_type}"
        )
