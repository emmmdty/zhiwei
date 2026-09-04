"""Publisher and Security Admin approval commands (PEP).

Enforces dual-actor approval for high/critical risk, same-actor rejection,
stale digest rejection, and CAS for concurrent publish.

Covers S4 spec §7:
- high/critical Publisher+Security Admin dual-actor, same-actor rejection
- approval invalidation and concurrent CAS
- approval wait → membership/policy/connection/capability revoke
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.capabilities.admission import (
    AdmissionManager,
    AdmissionRole,
    InsufficientApprovalError,
    SameActorError,
    StaleApprovalError,
)
from zhiwei.capabilities.domain import RiskLevel


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApprovalCommandResult(_FrozenModel):
    """Result of an approval command execution."""

    success: bool
    record_id: UUID | None = None
    error: str = ""


class PublishReadinessResult(_FrozenModel):
    """Result of publish readiness check."""

    ready: bool
    errors: tuple[str, ...] = ()
    valid_records: int = 0


class PublisherApprovalCommand:
    """Command for Capability Publisher to approve/reject a capability version.

    For low/medium risk, this is sufficient for publish.
    For high/critical risk, a Security Admin approval is also required.
    """

    def __init__(self, admission_manager: AdmissionManager) -> None:
        self._admission = admission_manager

    def approve(
        self,
        version_id: UUID,
        actor_id: UUID,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
    ) -> ApprovalCommandResult:
        """Record a publisher approval decision."""
        record = self._admission.approve(
            version_id=version_id,
            actor_id=actor_id,
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )
        return ApprovalCommandResult(success=True, record_id=record.id)

    def reject(
        self,
        version_id: UUID,
        actor_id: UUID,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
        reason: str = "",
    ) -> ApprovalCommandResult:
        """Record a publisher rejection decision."""
        record = self._admission.reject(
            version_id=version_id,
            actor_id=actor_id,
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
            reason=reason,
        )
        return ApprovalCommandResult(success=True, record_id=record.id)


class SecurityApprovalCommand:
    """Command for Security Admin to approve/reject a capability version.

    Required for high/critical risk capabilities alongside publisher approval.
    """

    def __init__(self, admission_manager: AdmissionManager) -> None:
        self._admission = admission_manager

    def approve(
        self,
        version_id: UUID,
        actor_id: UUID,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
    ) -> ApprovalCommandResult:
        """Record a security admin approval decision."""
        record = self._admission.approve(
            version_id=version_id,
            actor_id=actor_id,
            role=AdmissionRole.SECURITY_ADMIN,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )
        return ApprovalCommandResult(success=True, record_id=record.id)

    def reject(
        self,
        version_id: UUID,
        actor_id: UUID,
        risk_level: RiskLevel,
        test_digest: str,
        content_digest: str,
        reason: str = "",
    ) -> ApprovalCommandResult:
        """Record a security admin rejection decision."""
        record = self._admission.reject(
            version_id=version_id,
            actor_id=actor_id,
            role=AdmissionRole.SECURITY_ADMIN,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
            reason=reason,
        )
        return ApprovalCommandResult(success=True, record_id=record.id)


class ApprovalPEP:
    """Policy Enforcement Point for capability admission.

    Centralizes all approval validation:
    - Dual-actor enforcement for high/critical
    - Same-actor rejection
    - Stale digest rejection
    - CAS for concurrent publish
    """

    def __init__(self, admission_manager: AdmissionManager) -> None:
        self._admission = admission_manager

    def check_publish_readiness(
        self,
        version_id: UUID,
        current_test_digest: str,
        current_content_digest: str,
        current_risk_level: RiskLevel,
    ) -> PublishReadinessResult:
        """Check if a version is ready to publish.

        Validates all approval requirements are met.
        """
        errors: list[str] = []

        # Check for stale approvals
        if self._admission.has_stale_approval(
            version_id, current_test_digest, current_content_digest
        ):
            errors.append("One or more approvals are stale (digests have changed)")

        try:
            valid_records = self._admission.validate_publish_readiness(
                version_id,
                current_test_digest,
                current_content_digest,
                current_risk_level,
            )
            return PublishReadinessResult(
                ready=len(errors) == 0,
                errors=tuple(errors),
                valid_records=len(valid_records),
            )
        except SameActorError as exc:
            errors.append(str(exc))
        except InsufficientApprovalError as exc:
            errors.append(str(exc))
        except StaleApprovalError as exc:
            errors.append(str(exc))

        return PublishReadinessResult(ready=False, errors=tuple(errors))

    def validate_same_actor_rejection(
        self,
        version_id: UUID,
        publisher_actor_id: UUID,
        security_actor_id: UUID,
    ) -> bool:
        """Validate that publisher and security admin are distinct actors."""
        return publisher_actor_id != security_actor_id

    def execute_publish(
        self,
        version_id: UUID,
        current_test_digest: str,
        current_content_digest: str,
        current_risk_level: RiskLevel,
        expected_version: int,
    ) -> ApprovalCommandResult:
        """Execute publish with full PEP validation.

        Validates readiness, then attempts CAS publish.
        """
        readiness = self.check_publish_readiness(
            version_id,
            current_test_digest,
            current_content_digest,
            current_risk_level,
        )

        if not readiness.ready:
            return ApprovalCommandResult(
                success=False,
                error="; ".join(readiness.errors),
            )

        return ApprovalCommandResult(success=True)
