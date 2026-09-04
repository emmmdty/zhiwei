"""S2 runtime: approval request domain, lifecycle, digest binding。

事实源：design doc §4.3、S2-T5 plan。

ApprovalRequest is bound to exact input digest. Replacing input creates a new request,
not a mutation. Approver must be a different human principal from requester/modifier.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.contracts.identifiers import new_id


class ApprovalStatus(StrEnum):
    """Lifecycle states for an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ApprovalError(RuntimeError):
    """Invalid approval operation."""


class ApprovalRequest(BaseModel):
    """An approval request bound to an exact input digest.

    Replacing input produces a new request; mutation is forbidden.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    run_id: UUID
    task_id: str
    input_digest: str
    requester: str
    last_input_modifier: str
    effective_agent_identity: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class ApprovalRequestManager:
    """Manages the lifecycle of approval requests."""

    def __init__(self) -> None:
        self._requests: dict[UUID, ApprovalRequest] = {}

    def create(
        self,
        *,
        run_id: UUID,
        task_id: str,
        input_digest: str,
        requester: str,
        input_modifier: str,
        agent_identity: str,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Create a new approval request bound to the given input digest."""
        req = ApprovalRequest(
            id=new_id(),
            run_id=run_id,
            task_id=task_id,
            input_digest=input_digest,
            requester=requester,
            last_input_modifier=input_modifier,
            effective_agent_identity=agent_identity,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        self._requests[req.id] = req
        return req

    def replace(
        self,
        request_id: UUID,
        *,
        new_input_digest: str,
        new_modifier: str,
        new_agent_identity: str,
    ) -> ApprovalRequest:
        """Replace input → create a new request (not mutation)."""
        original = self.get(request_id)
        if original.status != ApprovalStatus.PENDING:
            raise ApprovalError(
                f"Cannot replace request in status '{original.status}'"
            )
        new_req = ApprovalRequest(
            id=new_id(),
            run_id=original.run_id,
            task_id=original.task_id,
            input_digest=new_input_digest,
            requester=original.requester,
            last_input_modifier=new_modifier,
            effective_agent_identity=new_agent_identity,
            expires_at=original.expires_at,
            created_at=datetime.now(UTC),
        )
        self._requests[new_req.id] = new_req
        return new_req

    def get(self, request_id: UUID) -> ApprovalRequest:
        """Get an approval request by ID."""
        req = self._requests.get(request_id)
        if req is None:
            raise ApprovalError(f"Approval request {request_id} not found")
        return req

    def approve(self, request_id: UUID, *, approver: str) -> ApprovalRequest:
        """Approve a pending request. Approver must differ from requester/modifier."""
        req = self.get(request_id)
        if req.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"Request already in status '{req.status}'")
        if req.expires_at is not None and req.expires_at < datetime.now(UTC):
            raise ApprovalError("Approval request has expired")
        if approver == req.requester or approver == req.last_input_modifier:
            raise ApprovalError(
                "Approver must be a different human principal from requester/modifier"
            )
        updated = req.model_copy(update={
            "status": ApprovalStatus.APPROVED,
            "approver": approver,
            "resolved_at": datetime.now(UTC),
        })
        self._requests[request_id] = updated
        return updated

    def reject(
        self, request_id: UUID, *, approver: str, reason: str
    ) -> ApprovalRequest:
        """Reject a pending request. Approver must differ from requester/modifier."""
        req = self.get(request_id)
        if req.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"Request already in status '{req.status}'")
        if req.expires_at is not None and req.expires_at < datetime.now(UTC):
            raise ApprovalError("Approval request has expired")
        if approver == req.requester or approver == req.last_input_modifier:
            raise ApprovalError(
                "Approver must be a different human principal from requester/modifier"
            )
        updated = req.model_copy(update={
            "status": ApprovalStatus.REJECTED,
            "approver": approver,
            "resolved_at": datetime.now(UTC),
        })
        self._requests[request_id] = updated
        return updated

    def revoke(self, request_id: UUID, *, revoked_by: str) -> ApprovalRequest:
        """Revoke a pending request."""
        req = self.get(request_id)
        if req.status != ApprovalStatus.PENDING:
            raise ApprovalError(
                f"Cannot revoke request in status '{req.status}'"
            )
        if req.expires_at is not None and req.expires_at < datetime.now(UTC):
            raise ApprovalError("Approval request has expired")
        updated = req.model_copy(update={
            "status": ApprovalStatus.REVOKED,
            "resolved_at": datetime.now(UTC),
        })
        self._requests[request_id] = updated
        return updated
