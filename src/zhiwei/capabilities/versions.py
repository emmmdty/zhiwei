"""Version lifecycle management for capability resources.

Immutable version lifecycle: discovered → quarantined → inspected → tested
→ approved → published → deprecated / suspended / revoked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from zhiwei.capabilities.domain import (
    CapabilityStatus,
    CapabilityVersion,
    RiskLevel,
)
from zhiwei.contracts.identifiers import new_id

# Valid transitions: from status -> set of allowed next statuses.
_VALID_TRANSITIONS: dict[CapabilityStatus, set[CapabilityStatus]] = {
    CapabilityStatus.DISCOVERED: {CapabilityStatus.QUARANTINED},
    CapabilityStatus.QUARANTINED: {CapabilityStatus.INSPECTED},
    CapabilityStatus.INSPECTED: {CapabilityStatus.TESTED},
    CapabilityStatus.TESTED: {CapabilityStatus.APPROVED},
    CapabilityStatus.APPROVED: {CapabilityStatus.PUBLISHED},
    CapabilityStatus.PUBLISHED: {
        CapabilityStatus.DEPRECATED,
        CapabilityStatus.SUSPENDED,
        CapabilityStatus.REVOKED,
    },
    CapabilityStatus.DEPRECATED: {CapabilityStatus.REVOKED},
    CapabilityStatus.SUSPENDED: {CapabilityStatus.PUBLISHED, CapabilityStatus.REVOKED},
    CapabilityStatus.REVOKED: set(),
}


class InvalidTransitionError(RuntimeError):
    """Invalid capability version state transition."""


class NotFoundError(RuntimeError):
    """Capability version not found."""


class VersionConflictError(RuntimeError):
    """CAS version conflict on concurrent publish."""


class StaleDigestError(RuntimeError):
    """Content or test digest has changed since approval."""


class CapabilityVersionManager:
    """Manages the lifecycle of capability versions.

    Tracks all lifecycle states. Enforces valid transitions and immutability
    after publish. Supports immediate suspend/revoke from published state.
    """

    def __init__(self) -> None:
        self._versions: dict[UUID, CapabilityVersion] = {}

    def _get_version(self, version_id: UUID) -> CapabilityVersion:
        if version_id not in self._versions:
            raise NotFoundError(f"Capability version {version_id} not found")
        return self._versions[version_id]

    def register(
        self,
        capability_type: str,
        name: str,
        risk_level: RiskLevel = RiskLevel.LOW,
        content_digest: str = "",
        test_digest: str = "",
        **kwargs: object,
    ) -> CapabilityVersion:
        """Register a new capability in discovered state."""
        now = datetime.now(UTC)
        metadata: dict[str, Any] = kwargs.get("metadata", {})  # type: ignore[assignment]
        parent_id: UUID | None = kwargs.get("parent_id")  # type: ignore[assignment]
        version = CapabilityVersion(
            id=new_id(),
            capability_type=capability_type,
            name=name,
            version=1,
            status=CapabilityStatus.DISCOVERED,
            risk_level=risk_level,
            content_digest=content_digest,
            test_digest=test_digest,
            metadata=metadata,
            parent_id=parent_id,
            created_at=now,
            updated_at=now,
        )
        self._versions[version.id] = version
        return version

    def transition(
        self,
        version_id: UUID,
        target: CapabilityStatus,
        *,
        expected_version: int | None = None,
    ) -> CapabilityVersion:
        """Transition a capability version to a new lifecycle state.

        For publish transitions, expected_version enables CAS to prevent
        concurrent publish conflicts.
        """
        version = self._get_version(version_id)
        allowed = _VALID_TRANSITIONS.get(version.status, set())
        if target not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition from {version.status} to {target}; "
                f"allowed: {sorted(a.value for a in allowed) or '(terminal)'}"
            )
        if (
            target == CapabilityStatus.PUBLISHED
            and expected_version is not None
            and version.version != expected_version
        ):
            raise VersionConflictError(
                f"CAS conflict: expected version {expected_version}, "
                f"actual {version.version}"
            )
        updated = version.model_copy(
            update={"status": target, "updated_at": datetime.now(UTC)}
        )
        self._versions[version_id] = updated
        return updated

    def is_published(self, version_id: UUID) -> bool:
        """Check if a capability version is published."""
        version = self._get_version(version_id)
        return version.status == CapabilityStatus.PUBLISHED

    def get_all_published(self) -> list[CapabilityVersion]:
        """Get all published capability versions."""
        return [v for v in self._versions.values() if v.status == CapabilityStatus.PUBLISHED]

    def suspend(self, version_id: UUID) -> CapabilityVersion:
        """Immediately suspend a published capability."""
        return self.transition(version_id, CapabilityStatus.SUSPENDED)

    def revoke(self, version_id: UUID) -> CapabilityVersion:
        """Immediately revoke a capability (published or deprecated)."""
        version = self._get_version(version_id)
        if version.status not in {
            CapabilityStatus.PUBLISHED,
            CapabilityStatus.DEPRECATED,
        }:
            raise InvalidTransitionError(
                f"Cannot revoke from {version.status}; "
                "only published or deprecated capabilities can be revoked"
            )
        return self.transition(version_id, CapabilityStatus.REVOKED)
