"""S8-T5 Action types and ActionReceipt for the Discover pipeline.

Feed shows status/owner/severity/supporting/contradicting/freshness/dedupe。
Triage → create Case → ask Ask for evidence → request tool action → approval → Resolution。
Resolution doesn't rewrite detector output。
Lesson candidate from resolution。

事实源：specs/s8-discover-actions.md §4、§6。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc
from zhiwei.runtime.approvals import ApprovalRequest, ApprovalRequestManager


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ActionType(StrEnum):
    """Typed tool action kinds — model proposes, human approves."""

    QUERY = "query"
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    NOTIFY = "notify"
    EXPORT = "export"


class ActionStatus(StrEnum):
    """ActionRequest lifecycle states."""

    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionRequest(_FrozenModel):
    """Immutable request to execute a typed tool action.

    模型只提出 action request，不直接执行。
    人工审批后才进入执行阶段。
    """

    id: UUID
    hypothesis_id: UUID
    case_id: UUID | None = None
    action_type: ActionType
    tool_name: str = Field(min_length=1, description="Tool to execute this action")
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, description="Why this action is needed")
    requested_by: str = Field(min_length=1)
    status: ActionStatus = ActionStatus.PROPOSED
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_action(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ActionReceipt(_FrozenModel):
    """Immutable receipt of a completed action execution.

    执行结果附加到 Case/Hypothesis，用于 audit trail。
    Resolution 不改写原 detector output——receipt 只记录执行事实。
    """

    id: UUID
    action_request_id: UUID
    hypothesis_id: UUID
    case_id: UUID | None = None
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error_message: str = ""
    executed_by: str = Field(min_length=1, description="Service or human that executed")
    executed_at: datetime
    approval_required: bool = True
    approved_by: str = ""
    approval_timestamp: datetime | None = None

    @field_validator("executed_at", "approval_timestamp", check_fields=False)
    @classmethod
    def _utc_receipt(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return ensure_utc(value)
        return value


class LessonCandidateFromAction(_FrozenModel):
    """A lesson derived from an action receipt, to be reviewed by Memory Steward.

    Resolution → lesson candidate 进入 Memory Center。
    """

    id: UUID
    action_receipt_id: UUID
    resolution_id: UUID
    hypothesis_id: UUID
    summary: str = Field(min_length=1)
    category: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_lesson(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def _action_input_digest(request: ActionRequest) -> str:
    """S2 审批与精确输入绑定：对 action 的决定相关内容做内容寻址。"""
    return digest_bytes(
        canonical_json(
            {
                "action_type": request.action_type.value,
                "tool_name": request.tool_name,
                "parameters": request.parameters,
                "rationale": request.rationale,
            }
        )
    )


class ActionManager:
    """Manages ActionRequest lifecycle and ActionReceipt generation.

    Triage → create Case → ask Ask for evidence → request tool action → approval → Resolution。
    审批决定组合复用 S2 ApprovalRequestManager（SoD：审批人 ≠ requester/modifier 的
    唯一事实实现）——approve 只消费 S2 已批准的决定并落账，discover 不维护第二套
    审批语义，requester 本人无法经由本管理器 approve 自己发起的 action。
    """

    def __init__(self, approvals: ApprovalRequestManager | None = None) -> None:
        self._requests: dict[UUID, ActionRequest] = {}
        self._receipts: dict[UUID, ActionReceipt] = {}
        self._approvals = approvals or ApprovalRequestManager()
        # request_id → S2 ApprovalRequest id：submit_for_approval 时建立绑定
        self._decision_ids: dict[UUID, UUID] = {}

    @property
    def requests(self) -> tuple[ActionRequest, ...]:
        return tuple(self._requests.values())

    @property
    def receipts(self) -> tuple[ActionReceipt, ...]:
        return tuple(self._receipts.values())

    def s2_decision(self, request_id: UUID) -> ApprovalRequest:
        """S2 审批决定查询（discover API 投影消费：decision id / input digest
        的单一事实源——API 层不复制 digest 计算或访问 manager 内部状态）。"""
        return self._approvals.get(self._decision_ids[request_id])

    def create_request(
        self,
        hypothesis_id: UUID,
        action_type: ActionType,
        tool_name: str,
        rationale: str,
        requested_by: str,
        *,
        case_id: UUID | None = None,
        parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActionRequest:
        """Create a new action request in PROPOSED status."""
        request = ActionRequest(
            id=new_id(),
            hypothesis_id=hypothesis_id,
            case_id=case_id,
            action_type=action_type,
            tool_name=tool_name,
            parameters=parameters or {},
            rationale=rationale,
            requested_by=requested_by,
            metadata=metadata or {},
            created_at=datetime.now(UTC),
        )
        self._requests[request.id] = request
        return request

    def submit_for_approval(self, request_id: UUID) -> ActionRequest:
        """Transition PROPOSED → PENDING_APPROVAL.

        进入审批即建立 S2 ApprovalRequest 绑定（requester = requested_by；action 由
        Discover 流程内的模型提出，effective agent identity 固定为 discover）。
        """
        request = self._get_request(request_id)
        if request.status != ActionStatus.PROPOSED:
            raise ValueError(
                f"Cannot submit for approval: request is in {request.status} status"
            )
        updated = request.model_copy(
            update={"status": ActionStatus.PENDING_APPROVAL, "created_at": request.created_at}
        )
        self._requests[request_id] = updated
        decision = self._approvals.create(
            run_id=request.hypothesis_id,
            task_id="discover-action",
            input_digest=_action_input_digest(request),
            requester=request.requested_by,
            input_modifier=request.requested_by,
            agent_identity="discover",
        )
        self._decision_ids[request_id] = decision.id
        return updated

    def approve(self, request_id: UUID, approved_by: str) -> ActionRequest:
        """Approve a pending action request — transitions PENDING_APPROVAL → APPROVED.

        审批决定经 S2 ApprovalRequestManager.approve 产生——审批人等于
        requester/modifier 时由 S2 拒绝（ApprovalError）；本方法只消费已批准的决定。
        """
        request = self._get_request(request_id)
        if request.status != ActionStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Cannot approve request in {request.status} status; "
                "only pending_approval can be approved"
            )
        self._approvals.approve(self._decision_ids[request_id], approver=approved_by)
        updated = request.model_copy(
            update={"status": ActionStatus.APPROVED, "created_at": request.created_at}
        )
        self._requests[request_id] = updated
        return updated

    def reject(self, request_id: UUID) -> ActionRequest:
        """Reject a pending action request — transitions PENDING_APPROVAL → REJECTED.

        拒绝是 fail-safe 方向，无 SoD 风险；S2 镜像决定保持 pending——request 已离开
        PENDING_APPROVAL，approve 不会再消费它。
        """
        request = self._get_request(request_id)
        if request.status != ActionStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Cannot reject request in {request.status} status; "
                "only pending_approval can be rejected"
            )
        updated = request.model_copy(
            update={"status": ActionStatus.REJECTED, "created_at": request.created_at}
        )
        self._requests[request_id] = updated
        return updated

    def record_receipt(
        self,
        request_id: UUID,
        *,
        success: bool,
        output: dict[str, Any] | None = None,
        error_message: str = "",
        executed_by: str,
        approved_by: str = "",
        approval_timestamp: datetime | None = None,
    ) -> ActionReceipt:
        """Record the execution receipt for an approved action request.

        Resolution 不改写原 detector output——receipt 只记录执行事实。
        """
        request = self._get_request(request_id)
        if request.status != ActionStatus.APPROVED:
            raise ValueError(
                f"Cannot record receipt for request in {request.status} status; "
                "only approved requests can produce receipts"
            )
        now = datetime.now(UTC)
        # 回执的审批人只消费 S2 审批决定——不得把 requester 回填成 approved_by。
        decision = self._approvals.get(self._decision_ids[request_id])
        receipt = ActionReceipt(
            id=new_id(),
            action_request_id=request_id,
            hypothesis_id=request.hypothesis_id,
            case_id=request.case_id,
            success=success,
            output=output or {},
            error_message=error_message,
            executed_by=executed_by,
            executed_at=now,
            approval_required=True,
            approved_by=approved_by or decision.approver or "",
            approval_timestamp=approval_timestamp or now,
        )
        completed_request = request.model_copy(
            update={
                "status": ActionStatus.COMPLETED if success else ActionStatus.FAILED,
                "created_at": request.created_at,
            }
        )
        self._requests[request_id] = completed_request
        self._receipts[receipt.id] = receipt
        return receipt

    def _get_request(self, request_id: UUID) -> ActionRequest:
        if request_id not in self._requests:
            raise ValueError(f"ActionRequest {request_id} not found")
        return self._requests[request_id]
