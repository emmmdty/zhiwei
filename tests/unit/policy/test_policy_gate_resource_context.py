"""F-R3-06 RED：authorize_mutation 的 resource_context 证据通道（REMEDIATION_PLAN T-P2a.2）。

finding 证据：api/policy_gate._build_policy_input 固定构造空 ResourceContext，
9 个冻结矩阵 cell 经生产 PEP 不可达——
- 边界强制证据类（PolicyInput validator 直接拒，policy/input.py _REQUIRED_*）：agent_publish.review_publish、
  tool_approval.approve/reject/replace、capability_version.review_high_critical；
- own 语义类（Rego not_owner 恒触发，authz.rego own_actions）：org.read_self、
  connection_secret.create_own/revoke_own、team_memory.submit_own_candidate。

RED 契约（本文件钉 PEP 通道的输入构造，不重复 Rego 语义——SoD/own 判定由
policies/zhiwei/authz_test.rego + 真实 OPA 套件与 tests/unit/policy/test_input.py 钉死）：
- authorize_mutation 接受 resource_context 关键字（PEP 职责：调用方从权威记录解析
  当事人证据后传入，与 risk 参数同一模式）；
- 证据必须原样抵达 enforcer 收到的 PolicyInput.resource_context；
- 不传 resource_context 时保持现状：证据类动作仍被 PolicyInput 边界拒绝（fail closed）。

actor 绑定只影响 binding_from_membership 的词表转换（未知角色 fail closed）；fake
enforcer 不做矩阵裁决，因此 allow 路径能隔离验证「证据通道」本身。
"""

from uuid import uuid4

import pytest
from fixtures.policy_fake import FakePolicyEnforcer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.api.policy_gate import (
    MutationAuthorization,
    _build_policy_input,
    authorize_mutation,
)
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.policy.input import ResourceContext
from zhiwei.policy.roles import Action, Purpose, ResourceType, Risk, Role

_ORG = uuid4()
_WS = uuid4()
_ACTOR_ID = uuid4()
_OTHER = uuid4()
_THIRD = uuid4()


def _actor(*bindings: ActorRoleBinding) -> ActorContext:
    return ActorContext(
        principal_id=_ACTOR_ID,
        organization_id=_ORG,
        workspace_id=_WS,
        role_bindings=bindings,
        active_organization_ids=(_ORG,),
    )


def _ws_admin() -> ActorContext:
    return _actor(
        ActorRoleBinding(
            name="workspace_admin", scope="workspace", organization_id=_ORG, workspace_id=_WS
        )
    )


def _mutate(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "bootstrap": False,
        "organization_id": _ORG,
        "workspace_id": _WS,
        "audit_action": "test.resource_context_channel",
        "resource_type": "test_resource",
        "resource_id": uuid4(),
        "resource_version": 1,
        "purpose": Purpose.GENERAL,
        "request_id": uuid4().hex,
        "trace_id": uuid4().hex,
    }
    base.update(kwargs)
    return base


async def _authorize_with_evidence(
    actor: ActorContext,
    policy_type: ResourceType,
    policy_action: Action,
    resource_context: ResourceContext,
    *,
    risk: Risk | None = None,
) -> tuple[FakePolicyEnforcer, MutationAuthorization]:
    """allow 路径不触碰 DB 会话（审计写由调用方在业务事务内做），bind 可省。"""
    enforcer = FakePolicyEnforcer(allow=True)
    authorization = await authorize_mutation(
        enforcer=enforcer,
        sessions=async_sessionmaker[AsyncSession](),
        actor=actor,
        policy_type=policy_type,
        policy_action=policy_action,
        resource_context=resource_context,
        risk=risk,
        **_mutate(),  # type: ignore[arg-type]
    )
    return enforcer, authorization


class TestEvidenceCellsReachable:
    """边界强制证据类 cell：证据经 resource_context 参数抵达 PolicyInput。"""

    @pytest.mark.asyncio
    async def test_review_publish_with_last_author_evidence(self) -> None:
        enforcer, authorization = await _authorize_with_evidence(
            _ws_admin(),
            ResourceType.AGENT_PUBLISH,
            Action.REVIEW_PUBLISH,
            ResourceContext(last_content_author_principal_id=_OTHER),
        )
        assert authorization.decision.allow is True
        observed = enforcer.inputs[0].resource_context
        assert observed.last_content_author_principal_id == _OTHER

    @pytest.mark.parametrize("action", [Action.APPROVE, Action.REJECT, Action.REPLACE])
    @pytest.mark.asyncio
    async def test_approval_cells_with_requester_evidence(self, action: Action) -> None:
        enforcer, authorization = await _authorize_with_evidence(
            _actor(
                ActorRoleBinding(name="approver", scope="org", organization_id=_ORG),
            ),
            ResourceType.TOOL_APPROVAL,
            action,
            ResourceContext(
                requester_principal_id=_OTHER,
                modifier_principal_ids=(_THIRD,),
            ),
        )
        assert authorization.decision.allow is True
        observed = enforcer.inputs[0].resource_context
        assert observed.requester_principal_id == _OTHER
        assert observed.modifier_principal_ids == (_THIRD,)

    @pytest.mark.asyncio
    async def test_review_high_critical_with_publisher_evidence(self) -> None:
        enforcer, authorization = await _authorize_with_evidence(
            _actor(
                ActorRoleBinding(name="security_admin", scope="org", organization_id=_ORG),
            ),
            ResourceType.CAPABILITY_VERSION,
            Action.REVIEW_HIGH_CRITICAL,
            ResourceContext(
                publisher_principal_id=_OTHER,
                publisher_roles=(Role.CAPABILITY_PUBLISHER,),
            ),
            risk=Risk.HIGH,
        )
        assert authorization.decision.allow is True
        observed = enforcer.inputs[0].resource_context
        assert observed.publisher_principal_id == _OTHER
        assert observed.publisher_roles == (Role.CAPABILITY_PUBLISHER,)


class TestOwnCellsReachable:
    """own 语义类 cell：owner 证据 == actor 时经 PEP 可达（rego not_owner 不触发）。"""

    @pytest.mark.parametrize(
        ("policy_type", "action"),
        [
            (ResourceType.ORG, Action.READ_SELF),
            (ResourceType.CONNECTION_SECRET, Action.CREATE_OWN),
            (ResourceType.CONNECTION_SECRET, Action.REVOKE_OWN),
            (ResourceType.TEAM_MEMORY, Action.SUBMIT_OWN_CANDIDATE),
        ],
    )
    @pytest.mark.asyncio
    async def test_own_cell_with_owner_evidence(
        self, policy_type: ResourceType, action: Action
    ) -> None:
        enforcer, authorization = await _authorize_with_evidence(
            _actor(
                ActorRoleBinding(name="member", scope="org", organization_id=_ORG),
            ),
            policy_type,
            action,
            ResourceContext(owner_principal_id=_ACTOR_ID),
        )
        assert authorization.decision.allow is True
        assert enforcer.inputs[0].resource_context.owner_principal_id == _ACTOR_ID


class TestFailClosedWithoutEvidence:
    """不传 resource_context 时保持现状：证据类动作在 PolicyInput 边界被拒。"""

    def test_review_publish_without_evidence_rejected_at_boundary(self) -> None:
        with pytest.raises(ValueError):
            _build_policy_input(
                actor=_ws_admin(),
                organization_id=_ORG,
                workspace_id=_WS,
                policy_type=ResourceType.AGENT_PUBLISH,
                policy_action=Action.REVIEW_PUBLISH,
                resource_id=uuid4(),
                resource_version=1,
                purpose=Purpose.GENERAL,
                trace_id=uuid4().hex,
            )

    def test_approval_without_evidence_rejected_at_boundary(self) -> None:
        with pytest.raises(ValueError):
            _build_policy_input(
                actor=_actor(
                    ActorRoleBinding(name="approver", scope="org", organization_id=_ORG),
                ),
                organization_id=_ORG,
                workspace_id=None,
                policy_type=ResourceType.TOOL_APPROVAL,
                policy_action=Action.APPROVE,
                resource_id=uuid4(),
                resource_version=1,
                purpose=Purpose.GENERAL,
                trace_id=uuid4().hex,
            )
