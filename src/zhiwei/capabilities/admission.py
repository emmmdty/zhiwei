"""Admission records and approval workflow for capability versions.

Dual-actor approval required for high/critical risk: Capability Publisher +
Security Admin. Same actor rejection, stale digest rejection, and CAS for
concurrent publish.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from zhiwei.capabilities.domain import RiskLevel
from zhiwei.contracts.time import ensure_utc


class AdmissionRole(StrEnum):
    """Roles that can approve capability admission."""

    CAPABILITY_PUBLISHER = "capability_publisher"
    SECURITY_ADMIN = "security_admin"


class ApprovalState(StrEnum):
    """Admission record states."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class AdmissionRecord(_FrozenModel):
    """Immutable record of an admission decision for a capability version.

    Each record captures actor, role, risk level, and digests at decision time.
    Any content/test/risk change invalidates existing approvals.
    """

    id: UUID
    version_id: UUID
    actor_id: UUID
    role: AdmissionRole
    decision: ApprovalState
    risk_level: RiskLevel
    test_digest: str
    content_digest: str
    reason: str = ""
    created_at: datetime
    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def is_valid_for_publish(
        self,
        current_test_digest: str,
        current_content_digest: str,
    ) -> bool:
        """Check if this admission record is still valid for publish.

        Valid only if digests match and the decision is approved.
        """
        return (
            self.decision == ApprovalState.APPROVED
            and self.test_digest == current_test_digest
            and self.content_digest == current_content_digest
        )

    def is_stale(
        self,
        current_test_digest: str,
        current_content_digest: str,
    ) -> bool:
        """Check if this admission record is stale (digests have changed)."""
        return (
            self.test_digest != current_test_digest
            or self.content_digest != current_content_digest
        )


class AdmissionError(RuntimeError):
    """Admission validation error."""


class SameActorError(AdmissionError):
    """Same actor cannot provide both publisher and security approvals."""


class StaleApprovalError(AdmissionError):
    """Approval is stale: content or test digest has changed."""


class InsufficientApprovalError(AdmissionError):
    """Not enough valid approvals for the required risk level."""


class ConcurrentPublishError(AdmissionError):
    """Concurrent publish conflict detected."""


class AdmissionManager:
    """Manages admission records and validates publish readiness.

    Enforces:
    - low/medium risk: single publisher approval sufficient
    - high/critical risk: require publisher + security admin (distinct actors)
    - same actor rejection for dual-actor approvals
    - stale digest rejection
    - CAS for concurrent publish
    """

    def __init__(self) -> None:
        self._records: dict[UUID, AdmissionRecord] = {}

    def _get_records_for_version(self, version_id: UUID) -> list[AdmissionRecord]:
        return [r for r in self._records.values() if r.version_id == version_id]

    def add_record(self, record: AdmissionRecord) -> None:
        """Add an admission record."""
        self._records[record.id] = record

    def approve(
        self,
        version_id: UUID,
        actor_id: UUID,
        role: AdmissionRole,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
    ) -> AdmissionRecord:
        """Record an approval decision."""
        from zhiwei.contracts.identifiers import new_id

        record = AdmissionRecord(
            id=new_id(),
            version_id=version_id,
            actor_id=actor_id,
            role=role,
            decision=ApprovalState.APPROVED,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
            created_at=datetime.now(UTC),
        )
        self._records[record.id] = record
        return record

    def reject(
        self,
        version_id: UUID,
        actor_id: UUID,
        role: AdmissionRole,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
        reason: str = "",
    ) -> AdmissionRecord:
        """Record a rejection decision."""
        from zhiwei.contracts.identifiers import new_id

        record = AdmissionRecord(
            id=new_id(),
            version_id=version_id,
            actor_id=actor_id,
            role=role,
            decision=ApprovalState.REJECTED,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self._records[record.id] = record
        return record

    def can_publish(
        self,
        version_id: UUID,
        current_test_digest: str,
        current_content_digest: str,
        current_risk_level: RiskLevel,
    ) -> bool:
        """Check if a version has sufficient valid approvals for publish."""
        try:
            self.validate_publish_readiness(
                version_id,
                current_test_digest,
                current_content_digest,
                current_risk_level,
            )
            return True
        except AdmissionError:
            return False

    def validate_publish_readiness(
        self,
        version_id: UUID,
        current_test_digest: str,
        current_content_digest: str,
        current_risk_level: RiskLevel,
    ) -> list[AdmissionRecord]:
        """Validate that a version has sufficient valid approvals for publish.

        Returns the list of valid approval records.
        Raises InsufficientApprovalError or SameActorError if not ready.
        """
        records = self._get_records_for_version(version_id)
        valid_records = [
            r
            for r in records
            if r.is_valid_for_publish(current_test_digest, current_content_digest)
        ]

        if current_risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return self._validate_dual_actor(valid_records, current_risk_level)
        return self._validate_single_actor(valid_records)

    def _validate_single_actor(
        self, valid_records: list[AdmissionRecord]
    ) -> list[AdmissionRecord]:
        """Validate single actor approval for low/medium risk."""
        approvals = [r for r in valid_records if r.decision == ApprovalState.APPROVED]
        if not approvals:
            raise InsufficientApprovalError("No valid approvals found")
        return approvals

    def _validate_dual_actor(
        self,
        valid_records: list[AdmissionRecord],
        risk_level: RiskLevel,
    ) -> list[AdmissionRecord]:
        """Validate dual actor approval for high/critical risk."""
        approvals = [r for r in valid_records if r.decision == ApprovalState.APPROVED]

        publisher_approvals = [
            r for r in approvals if r.role == AdmissionRole.CAPABILITY_PUBLISHER
        ]
        security_approvals = [
            r for r in approvals if r.role == AdmissionRole.SECURITY_ADMIN
        ]

        if not publisher_approvals:
            raise InsufficientApprovalError(
                f"No valid capability publisher approval for {risk_level} risk"
            )
        if not security_approvals:
            raise InsufficientApprovalError(
                f"No valid security admin approval for {risk_level} risk"
            )

        publisher_actor = publisher_approvals[0].actor_id
        security_actor = security_approvals[0].actor_id
        if publisher_actor == security_actor:
            raise SameActorError(
                "high/critical risk requires two distinct actors; "
                "same actor cannot provide both approvals"
            )

        return approvals

    def get_latest_approval(
        self,
        version_id: UUID,
    ) -> AdmissionRecord | None:
        """Get the most recent approval for a version."""
        records = self._get_records_for_version(version_id)
        approvals = [
            r
            for r in records
            if r.decision == ApprovalState.APPROVED
        ]
        if not approvals:
            return None
        return max(approvals, key=lambda r: r.created_at)

    def has_stale_approval(
        self,
        version_id: UUID,
        current_test_digest: str,
        current_content_digest: str,
    ) -> bool:
        """Check if any existing approval for a version is stale."""
        records = self._get_records_for_version(version_id)
        approvals = [
            r
            for r in records
            if r.decision == ApprovalState.APPROVED
        ]
        return any(
            r.is_stale(current_test_digest, current_content_digest) for r in approvals
        )
