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
from zhiwei.capabilities.inspection.contracts import detect_capability_drift
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
        # F-R9-06：CAS 语义的独立行版本。CapabilityVersion.version 是业务版本号
        # （register 固定 1，生命周期内不递增），不能承载乐观并发控制；row_version
        # 随每次 transition 递增，publish 的 expected_version 与之比较才构成真 CAS。
        self._row_versions: dict[UUID, int] = {}

    def _get_version(self, version_id: UUID) -> CapabilityVersion:
        if version_id not in self._versions:
            raise NotFoundError(f"Capability version {version_id} not found")
        return self._versions[version_id]

    def get(self, version_id: UUID) -> CapabilityVersion | None:
        """读取当前版本投影（API 面状态镜像用）；不存在返回 None。"""
        return self._versions.get(version_id)

    def attach_metadata(self, version_id: UUID, patch: dict[str, Any]) -> CapabilityVersion:
        """合并 metadata（inspect 报告落账等）；版本身份字段不可变。"""
        current = self._get_version(version_id)
        updated = current.model_copy(
            update={"metadata": {**current.metadata, **patch}}
        )
        self._versions[version_id] = updated
        return updated

    def row_version(self, version_id: UUID) -> int:
        """当前行版本（乐观并发控制的比较基准）。"""
        self._get_version(version_id)
        return self._row_versions.get(version_id, 1)

    def find_published_by_name(self, name: str) -> CapabilityVersion | None:
        """同名已发布版本中最新的一个（upstream candidate 的前驱）。"""
        predecessors = [
            v
            for v in self._versions.values()
            if v.name == name and v.status == CapabilityStatus.PUBLISHED
        ]
        if not predecessors:
            return None
        return max(predecessors, key=lambda v: v.created_at)

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
        self._row_versions[version.id] = version.version
        return version

    def check_transition(self, version_id: UUID, target: CapabilityStatus) -> None:
        """结构合法性预检（无副作用）：跳步/终态迁移在此拒绝，先于审批评估。

        API 层据此把「状态机不允许」（409）与「审批不足」（403）分离为不同语义。
        """
        version = self._get_version(version_id)
        allowed = _VALID_TRANSITIONS.get(version.status, set())
        if target not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition from {version.status} to {target}; "
                f"allowed: {sorted(a.value for a in allowed) or '(terminal)'}"
            )

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
        self.check_transition(version_id, target)
        if target == CapabilityStatus.PUBLISHED and expected_version is not None:
            current_row = self._row_versions.get(version_id, version.version)
            if current_row != expected_version:
                raise VersionConflictError(
                    f"CAS conflict: expected version {expected_version}, "
                    f"actual {current_row}"
                )
        updated = version.model_copy(
            update={"status": target, "updated_at": datetime.now(UTC)}
        )
        self._versions[version_id] = updated
        self._row_versions[version_id] = self._row_versions.get(version_id, 1) + 1
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


def prepare_upstream_candidate(
    *,
    manager: CapabilityVersionManager,
    name: str,
    content_digest: str,
) -> dict[str, Any]:
    """upstream update → candidate 注册参数（F-R9-03/T-P3.4）。

    同名已发布版本存在且 content_digest 变化时，返回 candidate 注册参数：
    parent_id 指向已发布版本（绑定不变，S4 spec §4），metadata 携带
    detect_capability_drift 报告（HIGH violation）。无前驱或内容未变返回空 dict
    ——按全新注册处理。
    """
    predecessor = manager.find_published_by_name(name)
    if predecessor is None or predecessor.content_digest == content_digest:
        return {}
    report = detect_capability_drift(
        bound_content_digest=predecessor.content_digest,
        current_content_digest=content_digest,
        bound_test_digest=predecessor.test_digest,
        current_test_digest="",
    )
    return {
        "parent_id": predecessor.id,
        "metadata": {
            "source": "upstream_update",
            "drift": report.model_dump(),
        },
    }
