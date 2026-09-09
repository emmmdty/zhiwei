"""S2-T5 RED: ApprovalRequest lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zhiwei.contracts.identifiers import new_id
from zhiwei.runtime.approvals import (
    ApprovalError,
    ApprovalRequestManager,
    ApprovalStatus,
)


def _make_manager() -> ApprovalRequestManager:
    return ApprovalRequestManager()


def _future_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _past_expiry() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


class TestApprovalRequestDigestBinding:
    """ApprovalRequest is bound to exact input digest."""

    def test_request_created_with_digest(self) -> None:
        mgr = _make_manager()
        digest = "abc123"
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest=digest,
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        assert req.input_digest == digest

    def test_different_digest_requires_new_request(self) -> None:
        mgr = _make_manager()
        run_id = new_id()
        r1 = mgr.create(
            run_id=run_id,
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        r2 = mgr.create(
            run_id=run_id,
            task_id="t1",
            input_digest="bbb",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        assert r1.id != r2.id
        assert r1.input_digest != r2.input_digest


class TestApprovalRequestReplace:
    """Replace input → new request (not mutation)."""

    def test_replace_creates_new_request(self) -> None:
        mgr = _make_manager()
        run_id = new_id()
        original = mgr.create(
            run_id=run_id,
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        replaced = mgr.replace(
            original.id,
            new_input_digest="bbb",
            new_modifier="bob",
            new_agent_identity="agent-v2",
        )
        assert replaced.id != original.id
        assert replaced.input_digest == "bbb"
        assert replaced.last_input_modifier == "bob"
        assert replaced.effective_agent_identity == "agent-v2"

    def test_replace_preserves_run_and_task(self) -> None:
        mgr = _make_manager()
        run_id = new_id()
        original = mgr.create(
            run_id=run_id,
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        replaced = mgr.replace(
            original.id,
            new_input_digest="bbb",
            new_modifier="bob",
            new_agent_identity="agent-v2",
        )
        assert replaced.run_id == run_id
        assert replaced.task_id == "t1"


class TestApprovalRequestTrackIdentity:
    """Track requester, last input modifier, effective AgentIdentity."""

    def test_tracks_requester(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        assert req.requester == "alice"

    def test_tracks_modifier(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="bob",
            agent_identity="agent-v1",
        )
        assert req.last_input_modifier == "bob"

    def test_tracks_agent_identity(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v2",
        )
        assert req.effective_agent_identity == "agent-v2"


class TestApproverDifferentFromRequester:
    """Approver must be different human principal from requester/modifier."""

    def test_approver_same_as_requester_rejected(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        with pytest.raises(ApprovalError, match="different human principal"):
            mgr.approve(req.id, approver="alice")

    def test_approver_same_as_modifier_rejected(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="bob",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        with pytest.raises(ApprovalError, match="different human principal"):
            mgr.approve(req.id, approver="bob")

    def test_approver_different_succeeds(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        approved = mgr.approve(req.id, approver="charlie")
        assert approved.status == ApprovalStatus.APPROVED
        assert approved.approver == "charlie"


class TestExpiryAndRevoke:
    """Expiry and revoke handling."""

    def test_expired_request_cannot_be_approved(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_past_expiry(),
        )
        with pytest.raises(ApprovalError, match="expired"):
            mgr.approve(req.id, approver="charlie")

    def test_expired_request_cannot_be_rejected(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_past_expiry(),
        )
        with pytest.raises(ApprovalError, match="expired"):
            mgr.reject(req.id, approver="charlie", reason="no")

    def test_expired_request_cannot_be_revoked(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_past_expiry(),
        )
        with pytest.raises(ApprovalError, match="expired"):
            mgr.revoke(req.id, revoked_by="alice")

    def test_revoke_sets_status(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        revoked = mgr.revoke(req.id, revoked_by="alice")
        assert revoked.status == ApprovalStatus.REVOKED

    def test_revoked_request_cannot_be_approved(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        mgr.revoke(req.id, revoked_by="alice")
        with pytest.raises(ApprovalError, match="revoked"):
            mgr.approve(req.id, approver="charlie")


class TestConcurrentDecisionCAS:
    """Concurrent approve/reject CAS — first writer wins."""

    def test_first_approve_wins(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        first = mgr.approve(req.id, approver="charlie")
        with pytest.raises(ApprovalError, match="already"):
            mgr.approve(req.id, approver="dave")
        assert first.status == ApprovalStatus.APPROVED

    def test_first_reject_wins(self) -> None:
        mgr = _make_manager()
        req = mgr.create(
            run_id=new_id(),
            task_id="t1",
            input_digest="aaa",
            requester="alice",
            input_modifier="alice",
            agent_identity="agent-v1",
            expires_at=_future_expiry(),
        )
        first = mgr.reject(req.id, approver="charlie", reason="no")
        with pytest.raises(ApprovalError, match="already"):
            mgr.reject(req.id, approver="dave", reason="no")
        assert first.status == ApprovalStatus.REJECTED
