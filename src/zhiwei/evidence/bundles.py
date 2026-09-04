"""S6 Evidence bundles.

A bundle groups claims and their associated evidence refs with metadata
for serialization and transport.

事实源：S6 spec §3。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc
from zhiwei.evidence.claims import Claim, InferenceClaim, RecommendationClaim
from zhiwei.evidence.refs import EvidenceRef


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceBundle(_FrozenModel):
    """A structured bundle of evidence refs and claims for an answer.

    Each ref appears at most once; claims reference refs by ref_id.
    """

    bundle_id: UUID
    answer_id: UUID
    evidence_refs: tuple[EvidenceRef, ...] = Field(default_factory=tuple)
    claims: tuple[Claim, ...] = Field(default_factory=tuple)
    created_at: datetime
    schema_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value

    def ref_ids(self) -> frozenset[UUID]:
        """Return the set of all ref_ids in this bundle."""
        return frozenset(ref.ref_id for ref in self.evidence_refs)

    def claim_ref_ids(self) -> frozenset[UUID]:
        """Return the union of all ref_ids referenced by claims."""
        ids: set[UUID] = set()
        for claim in self.claims:
            for ref in claim.evidence_refs:
                ids.add(ref.ref_id)
            if isinstance(claim, (InferenceClaim, RecommendationClaim)):
                for ref in claim.supporting_inputs:
                    ids.add(ref.ref_id)
                for ref in claim.contradicting_inputs:
                    ids.add(ref.ref_id)
        return frozenset(ids)
