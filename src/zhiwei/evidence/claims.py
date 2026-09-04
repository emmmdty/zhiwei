"""S6 Claim types: Fact, Quote, Inference, Recommendation.

Fact/Quote bind answer digest, code-point span/digest, claim type, canonical
value, and EvidenceRefs. Inference/Recommendation bind only supporting and
contradicting inputs.

事实源：S6 spec §3、ADR-003、ADR-006。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.contracts.time import ensure_utc
from zhiwei.evidence.canonical_values import CanonicalValue, ReproducibilityLevel
from zhiwei.evidence.refs import (
    EvidenceRef,
    validate_evidence_ref_level_supports_claim,
)


class ClaimType(StrEnum):
    """Claim type discriminator."""

    FACT = "Fact"
    QUOTE = "Quote"
    INFERENCE = "Inference"
    RECOMMENDATION = "Recommendation"


class ClaimStatus(StrEnum):
    """Claim lifecycle states."""

    DRAFT = "draft"
    FINAL = "final"
    REVOKED = "revoked"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CodeSpan(_FrozenModel):
    """A code-point span within a source file."""

    file_path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    column_start: int | None = Field(default=None, ge=0)
    column_end: int | None = Field(default=None, ge=0)

    @field_validator("line_end")
    @classmethod
    def _line_end_gte_start(cls, value: int, info: Any) -> int:
        start = info.data.get("line_start")
        if start is not None and value < start:
            raise ValueError("line_end must be >= line_start")
        return value


class _BaseClaim(_FrozenModel):
    """Fields common to all claim types."""

    claim_id: UUID
    claim_type: ClaimType
    status: ClaimStatus = ClaimStatus.DRAFT
    answer_id: UUID
    evidence_refs: tuple[EvidenceRef, ...] = ()
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

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


class FactClaim(_BaseClaim):
    """A factual claim backed by deterministic evidence.

    Binds answer_digest, code_span, code_digest, canonical_value.
    Requires replayable or copy_frozen evidence (ADR-003).
    """

    claim_type: ClaimType = ClaimType.FACT
    answer_digest: str = Field(min_length=1)
    code_span: CodeSpan | None = None
    code_digest: str | None = None
    canonical_value: CanonicalValue

    @field_validator("answer_digest", "code_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("sha256:"):
            raise ValueError("digest must use sha256: prefix")
        return value

    @model_validator(mode="after")
    def _evidence_level_supports_fact(self) -> FactClaim:
        for ref in self.evidence_refs:
            validate_evidence_ref_level_supports_claim(ref, "Fact")
        return self

    @model_validator(mode="after")
    def _fact_needs_evidence(self) -> FactClaim:
        if not self.evidence_refs:
            raise ValueError("Fact claim must have at least one evidence ref")
        return self


class QuoteClaim(_BaseClaim):
    """A verbatim quote claim backed by deterministic evidence.

    Binds answer_digest, code_span, code_digest, canonical_value, quote_text.
    Requires replayable or copy_frozen evidence (ADR-003).
    """

    claim_type: ClaimType = ClaimType.QUOTE
    answer_digest: str = Field(min_length=1)
    code_span: CodeSpan | None = None
    code_digest: str | None = None
    canonical_value: CanonicalValue
    quote_text: str = Field(min_length=1)

    @field_validator("answer_digest", "code_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("sha256:"):
            raise ValueError("digest must use sha256: prefix")
        return value

    @model_validator(mode="after")
    def _evidence_level_supports_quote(self) -> QuoteClaim:
        for ref in self.evidence_refs:
            validate_evidence_ref_level_supports_claim(ref, "Quote")
        return self

    @model_validator(mode="after")
    def _quote_needs_evidence(self) -> QuoteClaim:
        if not self.evidence_refs:
            raise ValueError("Quote claim must have at least one evidence ref")
        return self


class InferenceClaim(_BaseClaim):
    """An inference claim supported by input evidence.

    Only binds supporting_inputs and contradicting_inputs. Does NOT
    carry deterministic verification; must not claim verified status.
    """

    claim_type: ClaimType = ClaimType.INFERENCE
    supporting_inputs: tuple[EvidenceRef, ...] = Field(default_factory=tuple)
    contradicting_inputs: tuple[EvidenceRef, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _inference_needs_supporting(self) -> InferenceClaim:
        if not self.supporting_inputs:
            raise ValueError("Inference claim must have at least one supporting input")
        return self

    @model_validator(mode="after")
    def _inference_no_deterministic_verified(self) -> InferenceClaim:
        for ref in self.evidence_refs:
            if ref.reproducibility_level == ReproducibilityLevel.REFERENCE_ONLY:
                continue
            # Non-reference_only refs in evidence_refs are acceptable but
            # the claim itself must not claim deterministic verification.
        return self


class RecommendationClaim(_BaseClaim):
    """A recommendation claim supported by input evidence.

    Only binds supporting_inputs and contradicting_inputs. Does NOT
    carry deterministic verification.
    """

    claim_type: ClaimType = ClaimType.RECOMMENDATION
    supporting_inputs: tuple[EvidenceRef, ...] = Field(default_factory=tuple)
    contradicting_inputs: tuple[EvidenceRef, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _recommendation_needs_supporting(self) -> RecommendationClaim:
        if not self.supporting_inputs:
            raise ValueError("Recommendation claim must have at least one supporting input")
        return self


# Union type for all claim types
Claim = FactClaim | QuoteClaim | InferenceClaim | RecommendationClaim
