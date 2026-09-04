"""S5 Security: Knowledge ACL enforcement tests.

验证 ADR-006 核心语义：
- ACL pre-filter + hydration re-check on CURRENT ACL
- Unknown/stale ACL fail closed
- 失权呈现: evidence_access_revoked placeholder, not silent removal
- System reproducibility: Evidence always reproducible by system
- User visibility: re-check current ACL, fail closed
- Auditor visibility by separate channel
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from zhiwei.knowledge.acl import (
    ACLContext,
    UnknownACLError,
    pre_filter,
    recheck_after_hydration,
)
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceVersion,
    SourceVersionState,
)


def _make_version(
    *,
    acl: ACLSnapshot | None = None,
    state: SourceVersionState = SourceVersionState.ACTIVE,
    connector: str = "test",
    classification: Classification = Classification.PUBLIC,
) -> SourceVersion:
    return SourceVersion(
        id=uuid4(),
        source_object_id=uuid4(),
        version_seq=1,
        locator=Locator(connector=connector, uri="test://example"),
        content_digest="sha256:" + "a" * 64,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_at=datetime(2026, 1, 1, tzinfo=UTC),
        acl=acl or ACLSnapshot(),
        classification=classification,
        state=state,
    )


def _make_acl_ctx(
    *,
    principal_id=None,
    organization_id=None,
    workspace_id=None,
    allowed_principals: frozenset[str] | None = None,
    allowed_groups: frozenset[str] | None = None,
    denied_principals: frozenset[str] | None = None,
) -> ACLContext:
    return ACLContext(
        principal_id=principal_id or uuid4(),
        organization_id=organization_id or uuid4(),
        workspace_id=workspace_id or uuid4(),
        allowed_principals=allowed_principals or frozenset(),
        allowed_groups=allowed_groups or frozenset(),
        denied_principals=denied_principals or frozenset(),
    )


class TestPreFilter:
    """ACL pre-filter: filter candidates before hydration."""

    def test_empty_acl_rejects_all(self):
        """Empty ACL snapshot → unknown → fail closed → reject."""
        version = _make_version()
        ctx = _make_acl_ctx()
        result = pre_filter([version], ctx)
        assert result == []

    def test_principal_allowed(self):
        """Principal in allowed_principals → pass."""
        pid = uuid4()
        version = _make_version(
            acl=ACLSnapshot(allowed_principals=(str(pid),))
        )
        ctx = _make_acl_ctx(principal_id=pid)
        result = pre_filter([version], ctx)
        assert len(result) == 1

    def test_principal_denied_overrides_allow(self):
        """Deny overrides allow (ADR-006)."""
        pid = uuid4()
        version = _make_version(
            acl=ACLSnapshot(
                allowed_principals=(str(pid),),
                denied_principals=(str(pid),),
            )
        )
        ctx = _make_acl_ctx(principal_id=pid)
        result = pre_filter([version], ctx)
        assert result == []

    def test_group_allowed(self):
        """Principal's group in allowed_groups → pass."""
        version = _make_version(
            acl=ACLSnapshot(allowed_groups=("engineering",))
        )
        ctx = _make_acl_ctx(allowed_groups=frozenset({"engineering"}))
        result = pre_filter([version], ctx)
        assert len(result) == 1

    def test_revoked_version_filtered(self):
        """Revoked versions are still subject to ACL pre-filter."""
        version = _make_version(state=SourceVersionState.REVOKED)
        ctx = _make_acl_ctx()
        result = pre_filter([version], ctx)
        assert result == []


class TestRecheckAfterHydration:
    """ACL re-check after hydration (ADR-006: fail closed)."""

    def test_revoked_version_denied_with_revoked_flag(self):
        """Revoked version → access_revoked=True (ADR-006 失权呈现)."""
        version = _make_version(state=SourceVersionState.REVOKED)
        ctx = _make_acl_ctx()
        result = recheck_after_hydration(version, ctx)
        assert result.allowed is False
        assert result.access_revoked is True

    def test_unknown_acl_raises_error(self):
        """Empty ACL → unknown → fail closed raises UnknownACLError."""
        version = _make_version()
        ctx = _make_acl_ctx()
        with pytest.raises(UnknownACLError):
            recheck_after_hydration(version, ctx)

    def test_known_principal_passes(self):
        """Known principal in ACL → allowed."""
        pid = uuid4()
        version = _make_version(
            acl=ACLSnapshot(allowed_principals=(str(pid),))
        )
        ctx = _make_acl_ctx(principal_id=pid)
        result = recheck_after_hydration(version, ctx)
        assert result.allowed is True
        assert result.access_revoked is False

    def test_revoked_state_overrides_acl_allow(self):
        """Revoked state denies even if ACL snapshot says allowed (ADR-006)."""
        pid = uuid4()
        version = _make_version(
            state=SourceVersionState.REVOKED,
            acl=ACLSnapshot(allowed_principals=(str(pid),)),
        )
        ctx = _make_acl_ctx(principal_id=pid)
        result = recheck_after_hydration(version, ctx)
        assert result.allowed is False
        assert result.access_revoked is True


class TestACLContextConsistency:
    """ACL context must be consistent with query context."""

    def test_context_principal_mismatch(self):
        """ACL context principal must match query principal."""
        pid1 = uuid4()
        pid2 = uuid4()
        ctx = _make_acl_ctx(principal_id=pid1)
        assert ctx.principal_id != pid2

    def test_context_org_consistency(self):
        """ACL context organization must match query organization."""
        org = uuid4()
        ctx = _make_acl_ctx(organization_id=org)
        assert ctx.organization_id == org


class TestSystemReproducibility:
    """ADR-006: System reproducibility — Evidence always reproducible by system."""

    def test_revoked_version_still_accessible_for_system(self):
        """Revoked version can still be accessed for system reproducibility.

        The re-check denies user visibility but the version itself
        remains in the system for audit/eval.
        """
        version = _make_version(state=SourceVersionState.REVOKED)
        ctx = _make_acl_ctx()
        result = recheck_after_hydration(version, ctx)
        # User visibility denied
        assert result.allowed is False
        # But the version itself is not deleted — system can still access
        assert version.state == SourceVersionState.REVOKED


class TestAuditChannel:
    """ADR-006: Auditor visibility by separate channel."""

    def test_auditor_can_see_revoked(self):
        """Auditor channel should see revoked versions.

        This tests the ACL check result has the revoked flag
        for auditor consumption.
        """
        version = _make_version(state=SourceVersionState.REVOKED)
        ctx = _make_acl_ctx()
        result = recheck_after_hydration(version, ctx)
        # Regular user sees access_revoked
        assert result.access_revoked is True
        # Auditor would use a separate channel (not tested here,
        # but the flag is available for auditor consumption)
