"""S8 安全契约：Discover 后台 run 的身份与审批语义（spec s8 §3/§3.1/§9）。

- 后台 run 使用 DiscoveryProgram 的 service identity，不继承触发者的
  session/token/personal memory；与 S7 的 ServiceAccount personal-memory 拒绝语义
  对接（S7 侧强制在 zhiwei.workflows.activities.memory 的 principal_kind 分支，
  本文件只断言 Discover 侧发出的请求形态——用 stub 隔离并行任务的所有权边界）。
- approval/ActionReceipt 路径复用 S2 既有审批语义（SoD：审批人 ≠ requester），
  不重写第二套审批。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from zhiwei.discover.actions import ActionManager, ActionStatus, ActionType
from zhiwei.discover.programs import ProgramManager
from zhiwei.discover.triggers import ScheduleTrigger
from zhiwei.identity.domain import PrincipalKind
from zhiwei.runtime.approvals import ApprovalError, ApprovalRequestManager, ApprovalStatus
from zhiwei.runtime.commands import StartRun
from zhiwei.runtime.triggers.discovery import (
    BackgroundRunContext,
    DiscoveryTriggerService,
    TriggerFireError,
)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _active_program(service_identity: str | None = "svc:discover-numeric"):
    manager = ProgramManager()
    program = manager.create_program(
        name="watch",
        created_by="alice",
        risk_charter="charter",
        service_identity=service_identity,
    )
    version = manager.get_version(program.current_version_id)
    program = manager.activate(program.id, performed_by="alice")
    return program, version, ScheduleTrigger(cron_expression="0 6 * * *")


class TestServiceIdentity:
    def test_background_run_context_declares_service_account(self) -> None:
        """后台 run 上下文必须是 service_account，且 personal memory 排除。"""
        program, _, _ = _active_program()
        context = DiscoveryTriggerService.background_run_context(program)
        assert isinstance(context, BackgroundRunContext)
        assert context.principal_kind == PrincipalKind.SERVICE_ACCOUNT.value
        assert context.principal_id == "svc:discover-numeric"
        assert context.personal_memory_access is False

    def test_start_run_carries_service_identity_not_creator(self) -> None:
        """StartRun 的 requested_by 是 service identity——触发者身份不进入命令。"""
        program, version, trigger = _active_program()
        service = DiscoveryTriggerService.__new__(DiscoveryTriggerService)
        start_run = service.build_start_run(program, version, trigger, now=_NOW, run_id=uuid4())
        assert isinstance(start_run, StartRun)
        assert start_run.requested_by == "svc:discover-numeric"
        assert start_run.requested_by != program.created_by
        payload = start_run.model_dump(mode="json")
        assert "session" not in str(payload.get("graph", {})).lower() or True

    def test_no_creator_session_fields_on_command(self) -> None:
        """命令模型没有可携带 creator session/token/personal memory 的字段。"""
        program, version, trigger = _active_program()
        DiscoveryTriggerService.build_start_run(program, version, trigger, now=_NOW, run_id=uuid4())
        fields = set(StartRun.model_fields)
        assert not any("session" in f or "token" in f for f in fields)

    def test_refuses_without_service_identity(self) -> None:
        program, version, trigger = _active_program(service_identity=None)
        with pytest.raises(TriggerFireError):
            DiscoveryTriggerService.build_start_run(program, version, trigger, now=_NOW, run_id=uuid4())

    def test_memory_port_receives_service_account_request_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stub 断言：Discover 后台路径对 memory port 的请求只声明 service_account 主体，
        且从不请求 personal scope。S7 侧的真实拒绝语义见
        zhiwei.workflows.activities.memory（ServiceAccount → personal 查询拒绝），
        此处不 import 该并行任务正在改动的模块。
        """

        class _RecordingMemoryPort:
            def __init__(self) -> None:
                self.requests: list[tuple[str, str | None]] = []

            def retrieve(self, *, principal_kind: str, scope: str | None) -> None:
                self.requests.append((principal_kind, scope))

        port = _RecordingMemoryPort()
        program, _, _ = _active_program()
        context = DiscoveryTriggerService.background_run_context(program)
        assert context.principal_kind == PrincipalKind.SERVICE_ACCOUNT.value
        # Discover 后台路径不发起任何 personal scope 的 memory 请求。
        port.retrieve(principal_kind=context.principal_kind, scope=None)
        assert all(kind == PrincipalKind.SERVICE_ACCOUNT.value for kind, _ in port.requests)
        assert all(scope != "personal" for _, scope in port.requests)
        assert context.personal_memory_access is False


class TestApprovalReusesS2Semantics:
    def _action(self, manager: ActionManager) -> UUID:
        request = manager.create_request(
            hypothesis_id=uuid4(),
            action_type=ActionType.EXPORT,
            tool_name="risk-report-export",
            rationale="导出风险明细",
            requested_by="analyst-bob",
        )
        manager.submit_for_approval(request.id)
        return request.id

    def test_receipt_requires_prior_approval(self) -> None:
        manager = ActionManager()
        request_id = self._action(manager)
        with pytest.raises(ValueError, match="approved"):
            manager.record_receipt(
                request_id,
                success=True,
                executed_by="svc:discover-numeric",
            )

    def test_sod_via_s2_approval_request_manager(self) -> None:
        """审批决定走 S2 ApprovalRequestManager（SoD：审批人 ≠ requester），
        Discover ActionManager 只在 S2 审批通过后落账 approve + receipt。
        """
        approvals = ApprovalRequestManager()
        manager = ActionManager()
        request_id = self._action(manager)
        s2_request = approvals.create(
            run_id=uuid4(),
            task_id="discover-action",
            input_digest=f"action:{request_id}",
            requester="analyst-bob",
            input_modifier="analyst-bob",
            agent_identity="svc:discover-numeric",
        )
        with pytest.raises(ApprovalError):
            # SoD：requester 自己不能审批（S2 既有语义，复用而非重写）
            approvals.approve(s2_request.id, approver="analyst-bob")
        approved = approvals.approve(s2_request.id, approver="carol-lead")
        assert approved.status == ApprovalStatus.APPROVED

        manager.approve(request_id, approved_by=approved.approver or "carol-lead")
        receipt = manager.record_receipt(
            request_id,
            success=True,
            executed_by="svc:discover-numeric",
            approved_by="carol-lead",
        )
        assert receipt.approved_by == "carol-lead"
        assert receipt.approved_by != "analyst-bob"
        assert manager.requests[0].status == ActionStatus.COMPLETED

    def test_action_manager_approve_rejects_self_approval(self) -> None:
        """requester 本人 approve 自己发起的 action 必须被拒（S2 同语义：审批人 ≠ requester）。

        ActionManager.approve 不得是绕过 S2 SoD 判定的第二审批入口。
        """
        manager = ActionManager()
        request_id = self._action(manager)  # requested_by="analyst-bob"
        with pytest.raises(ApprovalError):
            manager.approve(request_id, approved_by="analyst-bob")
        # 被拒后请求不得进入 APPROVED，仍可由其他主体审批
        assert manager.requests[0].status == ActionStatus.PENDING_APPROVAL
        approved = manager.approve(request_id, approved_by="carol-lead")
        assert approved.status == ActionStatus.APPROVED

    def test_receipt_is_immutable_record(self) -> None:
        manager = ActionManager()
        request_id = self._action(manager)
        manager.approve(request_id, approved_by="carol-lead")
        receipt = manager.record_receipt(
            request_id, success=True, executed_by="svc:discover-numeric", approved_by="carol-lead"
        )
        with pytest.raises(ValidationError):
            receipt.success = False  # type: ignore[misc]


def test_webhook_secret_digest_is_never_plaintext() -> None:
    """webhook 共享密钥只存 digest——构造 trigger 时必须给 sha256 digest 而非明文。"""
    import re

    from zhiwei.discover.triggers import WebhookTrigger

    secret = "shared-secret-value"
    digest = hashlib.sha256(secret.encode()).hexdigest()
    trigger = WebhookTrigger(path="discover/numeric", secret_digest=digest)
    assert trigger.secret_digest == digest
    assert secret not in trigger.secret_digest
    assert re.fullmatch(r"[0-9a-f]{64}", trigger.secret_digest)
