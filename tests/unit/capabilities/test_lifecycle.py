"""S4-T1 RED: Capability resource and lifecycle tests.

Tests immutable versions, every lifecycle transition, publisher roles,
update diff, immediate suspend/revoke, admission records, dual-actor
approval, same-actor rejection, stale digest, CAS, and tenant repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.capabilities.admission import (
    AdmissionManager,
    AdmissionRecord,
    AdmissionRole,
    ApprovalState,
    SameActorError,
)
from zhiwei.capabilities.domain import (
    CapabilityBinding,
    CapabilityStatus,
    CapabilityVersion,
    ProviderVersion,
    RiskLevel,
    SkillVersion,
    ToolDefinitionVersion,
    WorkflowVersion,
)
from zhiwei.capabilities.repositories import (
    CapabilityBindingRepository,
    CapabilityRepository,
)
from zhiwei.capabilities.versions import (
    CapabilityVersionManager,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
)
from zhiwei.contracts.identifiers import new_id

# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


class TestProviderVersion:
    def test_creation_with_valid_fields(self) -> None:
        version = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test-provider",
            version=1,
            status=CapabilityStatus.DISCOVERED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.name == "test-provider"
        assert version.version == 1
        assert version.status == CapabilityStatus.DISCOVERED

    def test_frozen_model_rejects_field_mutation(self) -> None:
        version = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test-provider",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.name = "changed"  # type: ignore[misc]

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            ProviderVersion(
                id=new_id(),
                provider_id=new_id(),
                name="",
                version=1,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_rejects_negative_version(self) -> None:
        with pytest.raises(ValidationError):
            ProviderVersion(
                id=new_id(),
                provider_id=new_id(),
                name="test",
                version=0,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_content_digest_is_computed(self) -> None:
        version = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test-provider",
            version=1,
            content={"key": "value"},
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.content_digest.startswith("sha256:")

    def test_content_digest_changes_with_content(self) -> None:
        v1 = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test",
            version=1,
            content={"key": "value"},
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        v2 = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test",
            version=1,
            content={"key": "different"},
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert v1.content_digest != v2.content_digest

    def test_immutability_after_creation(self) -> None:
        version = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test",
            version=1,
            status=CapabilityStatus.PUBLISHED,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.version = 2  # type: ignore[misc]


class TestToolDefinitionVersion:
    def test_creation(self) -> None:
        version = ToolDefinitionVersion(
            id=new_id(),
            provider_version_id=new_id(),
            tool_name="search",
            tool_type="mcp_tool",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.tool_name == "search"
        assert version.tool_type == "mcp_tool"

    def test_frozen(self) -> None:
        version = ToolDefinitionVersion(
            id=new_id(),
            provider_version_id=new_id(),
            tool_name="search",
            tool_type="mcp_tool",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.tool_name = "other"  # type: ignore[misc]

    def test_rejects_empty_tool_name(self) -> None:
        with pytest.raises(ValidationError):
            ToolDefinitionVersion(
                id=new_id(),
                provider_version_id=new_id(),
                tool_name="",
                tool_type="mcp_tool",
                version=1,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                updated_at=datetime(2025, 1, 1, tzinfo=UTC),
            )


class TestSkillVersion:
    def test_creation(self) -> None:
        version = SkillVersion(
            id=new_id(),
            skill_id=new_id(),
            name="summarizer",
            version=1,
            allowed_tools=("search", "write"),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.allowed_tools == ("search", "write")

    def test_frozen(self) -> None:
        version = SkillVersion(
            id=new_id(),
            skill_id=new_id(),
            name="summarizer",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.name = "other"  # type: ignore[misc]


class TestWorkflowVersion:
    def test_creation(self) -> None:
        version = WorkflowVersion(
            id=new_id(),
            workflow_id=new_id(),
            name="deploy",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.name == "deploy"

    def test_frozen(self) -> None:
        version = WorkflowVersion(
            id=new_id(),
            workflow_id=new_id(),
            name="deploy",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.name = "other"  # type: ignore[misc]


class TestCapabilityVersion:
    def test_creation(self) -> None:
        version = CapabilityVersion(
            id=new_id(),
            capability_type="mcp",
            name="test-cap",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert version.capability_type == "mcp"
        assert version.status == CapabilityStatus.DISCOVERED

    def test_frozen(self) -> None:
        version = CapabilityVersion(
            id=new_id(),
            capability_type="mcp",
            name="test-cap",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            version.name = "other"  # type: ignore[misc]


class TestCapabilityBinding:
    def test_creation(self) -> None:
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert binding.status == "active"

    def test_frozen(self) -> None:
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            binding.status = "inactive"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Version lifecycle tests
# ---------------------------------------------------------------------------


class TestCapabilityVersionManager:
    def test_register_discovered(self) -> None:
        mgr = CapabilityVersionManager()
        version = mgr.register("mcp", "test-tool")
        assert version.status == CapabilityStatus.DISCOVERED
        assert version.version == 1

    def test_full_lifecycle(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        assert v.status == CapabilityStatus.QUARANTINED
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        assert v.status == CapabilityStatus.INSPECTED
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        assert v.status == CapabilityStatus.TESTED
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        assert v.status == CapabilityStatus.APPROVED
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        assert v.status == CapabilityStatus.PUBLISHED

    def test_skip_step_rejected(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        with pytest.raises(InvalidTransitionError):
            mgr.transition(v.id, CapabilityStatus.INSPECTED)

    def test_suspend_from_published(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.suspend(v.id)
        assert v.status == CapabilityStatus.SUSPENDED

    def test_revoke_from_published(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.revoke(v.id)
        assert v.status == CapabilityStatus.REVOKED

    def test_deprecate_from_published(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.transition(v.id, CapabilityStatus.DEPRECATED)
        assert v.status == CapabilityStatus.DEPRECATED

    def test_revoke_from_deprecated(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.transition(v.id, CapabilityStatus.DEPRECATED)
        v = mgr.revoke(v.id)
        assert v.status == CapabilityStatus.REVOKED

    def test_republish_from_suspended(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.suspend(v.id)
        assert v.status == CapabilityStatus.SUSPENDED
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        assert v.status == CapabilityStatus.PUBLISHED

    def test_revoked_is_terminal(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        v = mgr.revoke(v.id)
        with pytest.raises(InvalidTransitionError):
            mgr.transition(v.id, CapabilityStatus.PUBLISHED)

    def test_not_found_raises(self) -> None:
        mgr = CapabilityVersionManager()
        with pytest.raises(NotFoundError):
            mgr.transition(new_id(), CapabilityStatus.QUARANTINED)

    def test_cas_publish_conflict(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        with pytest.raises(VersionConflictError):
            mgr.transition(v.id, CapabilityStatus.PUBLISHED, expected_version=999)

    def test_cas_publish_success(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED, expected_version=v.version)
        assert v.status == CapabilityStatus.PUBLISHED

    def test_is_published(self) -> None:
        mgr = CapabilityVersionManager()
        v = mgr.register("mcp", "tool")
        assert not mgr.is_published(v.id)
        v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
        v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
        v = mgr.transition(v.id, CapabilityStatus.TESTED)
        v = mgr.transition(v.id, CapabilityStatus.APPROVED)
        v = mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        assert mgr.is_published(v.id)

    def test_get_all_published(self) -> None:
        mgr = CapabilityVersionManager()
        v1 = mgr.register("mcp", "tool1")
        v2 = mgr.register("mcp", "tool2")
        for v in [v1, v2]:
            v = mgr.transition(v.id, CapabilityStatus.QUARANTINED)
            v = mgr.transition(v.id, CapabilityStatus.INSPECTED)
            v = mgr.transition(v.id, CapabilityStatus.TESTED)
            v = mgr.transition(v.id, CapabilityStatus.APPROVED)
            mgr.transition(v.id, CapabilityStatus.PUBLISHED)
        published = mgr.get_all_published()
        assert len(published) == 2

    def test_upstream_update_creates_candidate_not_bound(self) -> None:
        """S4 spec: upstream update creates candidate, doesn't change bound AgentVersion."""
        mgr = CapabilityVersionManager()
        v1 = mgr.register("mcp", "tool")
        v1 = mgr.transition(v1.id, CapabilityStatus.QUARANTINED)
        v1 = mgr.transition(v1.id, CapabilityStatus.INSPECTED)
        v1 = mgr.transition(v1.id, CapabilityStatus.TESTED)
        v1 = mgr.transition(v1.id, CapabilityStatus.APPROVED)
        v1 = mgr.transition(v1.id, CapabilityStatus.PUBLISHED)
        # Upstream update creates a new candidate version
        v2 = mgr.register("mcp", "tool")
        assert v2.status == CapabilityStatus.DISCOVERED
        assert v2.id != v1.id
        # v1 remains published (not changed by upstream update)
        assert mgr.is_published(v1.id)


# ---------------------------------------------------------------------------
# Admission tests
# ---------------------------------------------------------------------------


class TestAdmissionRecord:
    def test_valid_for_publish(self) -> None:
        record = AdmissionRecord(
            id=new_id(),
            version_id=new_id(),
            actor_id=new_id(),
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            decision=ApprovalState.APPROVED,
            risk_level=RiskLevel.LOW,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert record.is_valid_for_publish("sha256:aaa", "sha256:bbb")

    def test_stale_digest_invalid(self) -> None:
        record = AdmissionRecord(
            id=new_id(),
            version_id=new_id(),
            actor_id=new_id(),
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            decision=ApprovalState.APPROVED,
            risk_level=RiskLevel.LOW,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert not record.is_valid_for_publish("sha256:aaa", "sha256:ccc")

    def test_rejected_not_valid(self) -> None:
        record = AdmissionRecord(
            id=new_id(),
            version_id=new_id(),
            actor_id=new_id(),
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            decision=ApprovalState.REJECTED,
            risk_level=RiskLevel.LOW,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert not record.is_valid_for_publish("sha256:aaa", "sha256:bbb")

    def test_frozen(self) -> None:
        record = AdmissionRecord(
            id=new_id(),
            version_id=new_id(),
            actor_id=new_id(),
            role=AdmissionRole.CAPABILITY_PUBLISHER,
            decision=ApprovalState.APPROVED,
            risk_level=RiskLevel.LOW,
            test_digest="sha256:aaa",
            content_digest="sha256:bbb",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(ValidationError):
            record.decision = ApprovalState.REJECTED  # type: ignore[misc]


class TestAdmissionManager:
    def test_low_risk_single_publisher_approval(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.LOW, "sha256:test", "sha256:content",
        )
        assert mgr.can_publish(vid, "sha256:test", "sha256:content", RiskLevel.LOW)

    def test_high_risk_requires_dual_actor(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        publisher = new_id()
        security = new_id()
        mgr.approve(
            vid, publisher, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.HIGH, "sha256:test", "sha256:content",
        )
        # Only publisher, no security admin — should fail
        assert not mgr.can_publish(vid, "sha256:test", "sha256:content", RiskLevel.HIGH)
        mgr.approve(
            vid, security, AdmissionRole.SECURITY_ADMIN,
            RiskLevel.HIGH, "sha256:test", "sha256:content",
        )
        assert mgr.can_publish(vid, "sha256:test", "sha256:content", RiskLevel.HIGH)

    def test_same_actor_rejected_for_high_risk(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.HIGH, "sha256:test", "sha256:content",
        )
        mgr.approve(
            vid, actor, AdmissionRole.SECURITY_ADMIN,
            RiskLevel.HIGH, "sha256:test", "sha256:content",
        )
        with pytest.raises(SameActorError):
            mgr.validate_publish_readiness(
                vid, "sha256:test", "sha256:content", RiskLevel.HIGH,
            )

    def test_stale_digest_rejected(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.LOW, "sha256:test", "sha256:content",
        )
        # Content has changed since approval
        assert not mgr.can_publish(vid, "sha256:test", "sha256:newcontent", RiskLevel.LOW)

    def test_critical_risk_dual_actor(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        p = new_id()
        s = new_id()
        mgr.approve(
            vid, p, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.CRITICAL, "sha256:t", "sha256:c",
        )
        mgr.approve(
            vid, s, AdmissionRole.SECURITY_ADMIN,
            RiskLevel.CRITICAL, "sha256:t", "sha256:c",
        )
        assert mgr.can_publish(vid, "sha256:t", "sha256:c", RiskLevel.CRITICAL)

    def test_medium_risk_single_publisher(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.MEDIUM, "sha256:test", "sha256:content",
        )
        assert mgr.can_publish(vid, "sha256:test", "sha256:content", RiskLevel.MEDIUM)

    def test_reject_records_rejection(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        record = mgr.reject(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.LOW, "sha256:test", "sha256:content", "not ready",
        )
        assert record.decision == ApprovalState.REJECTED
        assert record.reason == "not ready"
        assert not mgr.can_publish(vid, "sha256:test", "sha256:content", RiskLevel.LOW)

    def test_get_latest_approval(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        r1 = mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.LOW, "sha256:test", "sha256:content",
        )
        latest = mgr.get_latest_approval(vid)
        assert latest is not None
        assert latest.id == r1.id

    def test_has_stale_approval(self) -> None:
        mgr = AdmissionManager()
        vid = new_id()
        actor = new_id()
        mgr.approve(
            vid, actor, AdmissionRole.CAPABILITY_PUBLISHER,
            RiskLevel.LOW, "sha256:test", "sha256:content",
        )
        assert not mgr.has_stale_approval(vid, "sha256:test", "sha256:content")
        assert mgr.has_stale_approval(vid, "sha256:test", "sha256:newcontent")


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


class TestCapabilityRepository:
    def test_store_and_retrieve(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        version = CapabilityVersion(
            id=new_id(),
            capability_type="mcp",
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_capability_version(version)
        assert repo.get_capability_version(version.id) is version

    def test_returns_none_for_missing(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        assert repo.get_capability_version(new_id()) is None

    def test_store_provider_version(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        version = ProviderVersion(
            id=new_id(),
            provider_id=new_id(),
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_provider_version(version)
        assert repo.get_provider_version(version.id) is version

    def test_store_tool_version(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        version = ToolDefinitionVersion(
            id=new_id(),
            provider_version_id=new_id(),
            tool_name="search",
            tool_type="mcp",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_tool_version(version)
        assert repo.get_tool_version(version.id) is version

    def test_store_skill_version(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        version = SkillVersion(
            id=new_id(),
            skill_id=new_id(),
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_skill_version(version)
        assert repo.get_skill_version(version.id) is version

    def test_store_workflow_version(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        version = WorkflowVersion(
            id=new_id(),
            workflow_id=new_id(),
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_workflow_version(version)
        assert repo.get_workflow_version(version.id) is version

    def test_remove(self) -> None:
        repo = CapabilityRepository(new_id(), new_id())
        vid = new_id()
        version = CapabilityVersion(
            id=vid,
            capability_type="mcp",
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.store_capability_version(version)
        repo.remove(vid)
        assert repo.get_capability_version(vid) is None

    def test_tenant_isolation(self) -> None:
        org1, ws1 = new_id(), new_id()
        org2, ws2 = new_id(), new_id()
        repo1 = CapabilityRepository(org1, ws1)
        repo2 = CapabilityRepository(org2, ws2)
        version = CapabilityVersion(
            id=new_id(),
            capability_type="mcp",
            name="test",
            version=1,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo1.store_capability_version(version)
        assert repo2.get_capability_version(version.id) is None


class TestCapabilityBindingRepository:
    def test_add_and_get(self) -> None:
        repo = CapabilityBindingRepository(new_id(), new_id())
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.add(binding)
        assert repo.get(binding.id) is binding

    def test_list_by_agent(self) -> None:
        repo = CapabilityBindingRepository(new_id(), new_id())
        agent_id = new_id()
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=agent_id,
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.add(binding)
        result = repo.list_by_agent(agent_id)
        assert len(result) == 1
        assert result[0].id == binding.id

    def test_list_by_capability(self) -> None:
        repo = CapabilityBindingRepository(new_id(), new_id())
        cap_id = new_id()
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=cap_id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.add(binding)
        result = repo.list_by_capability(cap_id)
        assert len(result) == 1

    def test_remove_binding(self) -> None:
        repo = CapabilityBindingRepository(new_id(), new_id())
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.add(binding)
        repo.remove(binding.id)
        assert repo.get(binding.id) is None

    def test_tenant_isolation(self) -> None:
        repo1 = CapabilityBindingRepository(new_id(), new_id())
        repo2 = CapabilityBindingRepository(new_id(), new_id())
        binding = CapabilityBinding(
            id=new_id(),
            organization_id=new_id(),
            workspace_id=new_id(),
            agent_definition_id=new_id(),
            agent_version_id=new_id(),
            capability_version_id=new_id(),
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo1.add(binding)
        assert repo2.get(binding.id) is None
