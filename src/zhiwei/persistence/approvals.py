"""S2-T7：ApprovalRequest 的 PG 持久化（审批旅程的跨账号可见性）。

事实源：specs/s2-agent-runtime.md §4/§5、S2 plan T5/T7、migrations/0011。

域契约（zhiwei.runtime.approvals：digest 绑定、replace 即新请求、approver ≠
requester/modifier、过期即拒、CAS 决策）不变；本模块把内存管理器换成
canonical PG 行（0011 表，FORCE RLS）。决策经 `UPDATE ... WHERE status='pending'`
的 CAS 语义实现，与终态触发器（0011）构成双层守护。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.persistence.models import ApprovalRequestRow
from zhiwei.persistence.tenant import TenantContext
from zhiwei.runtime.approvals import ApprovalError, ApprovalStatus


class ApprovalRequestRecord(BaseModel):
    """PG 行的不可变投影（供 API/workflow 消费）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    run_id: UUID
    task_id: str
    input_digest: str
    requester: str
    last_input_modifier: str
    agent_identity: str
    status: str
    requested_by: str
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None


def _record_from_row(row: ApprovalRequestRow) -> ApprovalRequestRecord:
    return ApprovalRequestRecord(
        request_id=row.id,
        run_id=row.run_id,
        task_id=row.task_id,
        input_digest=row.input_digest,
        requester=row.requester,
        last_input_modifier=row.last_input_modifier,
        agent_identity=row.agent_identity,
        status=row.status,
        requested_by=row.requested_by,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
    )


class ApprovalRequestStore:
    """0011 表上的审批请求存储（tenant 显式作用域）。"""

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if context.workspace_id is None:
            raise ApprovalError("approval requests require workspace context")
        self._session = session
        self._context = context

    async def create(
        self,
        *,
        run_id: UUID,
        task_id: str,
        input_digest: str,
        requester: str,
        agent_identity: str,
        requested_by: str,
        expires_at: datetime | None = None,
        request_id: UUID | None = None,
    ) -> ApprovalRequestRecord:
        """创建 pending 审批请求（workflow 的 create_approval activity 调用）。"""
        row = ApprovalRequestRow(
            id=request_id or _new_request_id(),
            organization_id=self._context.organization_id,
            workspace_id=self._context.workspace_id,
            run_id=run_id,
            task_id=task_id,
            input_digest=input_digest,
            requester=requester,
            last_input_modifier=requester,
            agent_identity=agent_identity,
            status=ApprovalStatus.PENDING.value,
            requested_by=requested_by,
            expires_at=expires_at,
            schema_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return _record_from_row(row)

    async def get(self, request_id: UUID) -> ApprovalRequestRecord:
        row = await self._session.get(ApprovalRequestRow, request_id)
        if row is None or row.organization_id != self._context.organization_id:
            raise ApprovalError(f"approval request {request_id} not found in tenant scope")
        return _record_from_row(row)

    async def list_for_run(self, run_id: UUID, *, status: str | None = None) -> list[ApprovalRequestRecord]:
        statement = select(ApprovalRequestRow).where(
            ApprovalRequestRow.organization_id == self._context.organization_id,
            ApprovalRequestRow.workspace_id == self._context.workspace_id,
            ApprovalRequestRow.run_id == run_id,
        )
        if status is not None:
            statement = statement.where(ApprovalRequestRow.status == status)
        statement = statement.order_by(ApprovalRequestRow.id)
        rows = (await self._session.scalars(statement)).all()
        return [_record_from_row(row) for row in rows]

    async def decide(
        self,
        *,
        request_id: UUID,
        decision: str,
        approver: str,
        reason: str,
        now: datetime | None = None,
    ) -> ApprovalRequestRecord:
        """CAS 决策：pending → approved/rejected；SoD/过期/CAS 由本方法 + 触发器守护。"""
        if decision not in {"approved", "rejected"}:
            raise ApprovalError(f"invalid decision: {decision!r}")
        decided_at = now or datetime.now(tz=UTC)
        row = await self._locked(request_id)
        if row is None:
            raise ApprovalError(f"approval request {request_id} not found in tenant scope")
        if row.status != ApprovalStatus.PENDING.value:
            raise ApprovalError(f"Request already in status '{row.status}'")
        if row.expires_at is not None and row.expires_at < decided_at:
            raise ApprovalError("Approval request has expired")
        if approver == row.requester or approver == row.last_input_modifier:
            raise ApprovalError(
                "Approver must be a different human principal from requester/modifier"
            )
        row.status = decision
        row.decided_by = approver
        row.decision_reason = reason
        row.decided_at = decided_at
        await self._session.flush()
        return _record_from_row(row)

    async def revoke(self, *, request_id: UUID, revoked_by: str) -> ApprovalRequestRecord:
        decided_at = datetime.now(tz=UTC)
        row = await self._locked(request_id)
        if row is None:
            raise ApprovalError(f"approval request {request_id} not found in tenant scope")
        if row.status != ApprovalStatus.PENDING.value:
            raise ApprovalError(f"Cannot revoke request in status '{row.status}'")
        if row.expires_at is not None and row.expires_at < decided_at:
            raise ApprovalError("Approval request has expired")
        row.status = ApprovalStatus.REVOKED.value
        row.decided_by = revoked_by
        row.decision_reason = "revoked"
        row.decided_at = decided_at
        await self._session.flush()
        return _record_from_row(row)

    async def _locked(self, request_id: UUID) -> ApprovalRequestRow | None:
        return await self._session.scalar(
            select(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.id == request_id,
                ApprovalRequestRow.organization_id == self._context.organization_id,
                ApprovalRequestRow.workspace_id == self._context.workspace_id,
            )
            .with_for_update()
        )


def _new_request_id() -> UUID:
    from uuid import uuid4

    return uuid4()
