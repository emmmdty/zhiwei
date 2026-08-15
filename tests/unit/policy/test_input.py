"""S1-T3 RED：PolicyInput 严格类型与边界拒绝（MC-2/12/15）。

input.py 只做规范化与协议边界：未知枚举值、未知 (resource, action) 组合、
SoD 证据缺失、secrets 形状字段一律拒绝；角色→权限映射不在此层。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from zhiwei.identity.domain import PrincipalKind
from zhiwei.policy.input import (
    Actor,
    Delegation,
    EffectiveIdentity,
    PolicyInput,
    RequestContext,
    ResourceContext,
    ResourceRef,
    RoleBinding,
)
from zhiwei.policy.roles import Action, Purpose, ResourceType, Risk, Role, RoleScope

ORG = UUID("00000000-0000-0000-0000-000000000001")
WS = UUID("00000000-0000-0000-0000-000000000002")
RES = UUID("00000000-0000-0000-0000-00000000000a")
NOW = datetime(2026, 8, 15, 0, 0, 0, tzinfo=UTC)


def org_binding(role: Role = Role.ORG_OWNER) -> RoleBinding:
    return RoleBinding(
        name=role, scope=RoleScope.ORG, organization_id=ORG, workspace_id=None
    )


def ws_binding(role: Role = Role.WORKSPACE_ADMIN) -> RoleBinding:
    return RoleBinding(
        name=role, scope=RoleScope.WORKSPACE, organization_id=ORG, workspace_id=WS
    )


def base_input(**overrides) -> PolicyInput:
    values: dict = {
        "organization_id": ORG,
        "workspace_id": WS,
        "actor": Actor(principal_id=RES, kind=PrincipalKind.USER, roles=(org_binding(),)),
        "resource": ResourceRef(type=ResourceType.ORG, id=RES, version="v1"),
        "action": Action.MANAGE,
        "purpose": Purpose.GENERAL,
        "classification": None,
        "risk": None,
        "delegation": (),
        "resource_context": ResourceContext(),
        "context": RequestContext(now=NOW),
    }
    values.update(overrides)
    return PolicyInput(**values)


class TestBoundaryRejection:
    def test_unknown_role_rejected(self) -> None:
        # 未知 role 在边界拒绝（MC-2），不能透传给 OPA。未知值经 dict 走真实边界路径
        # （构造器签名会先于 pydantic 校验被类型系统/运行时拒绝，测不到边界行为）
        doc = base_input().model_dump(mode="python")
        doc["actor"]["roles"][0]["name"] = "superuser"
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_unknown_resource_type_rejected(self) -> None:
        doc = base_input().model_dump(mode="python")
        doc["resource"]["type"] = "wat"
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_unknown_action_rejected(self) -> None:
        doc = base_input().model_dump(mode="python")
        doc["action"] = "publsh"
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_unknown_scope_rejected(self) -> None:
        doc = base_input().model_dump(mode="python")
        doc["actor"]["roles"][0]["scope"] = "team"
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_unknown_classification_rejected(self) -> None:
        with pytest.raises(ValidationError):
            base_input(classification="TOP_SECRET")

    def test_unknown_risk_rejected(self) -> None:
        with pytest.raises(ValidationError):
            base_input(risk="critical2")

    def test_unknown_purpose_rejected(self) -> None:
        with pytest.raises(ValidationError):
            base_input(purpose="everything")

    def test_resource_action_pair_must_exist_in_schema(self) -> None:
        # org 资源上没有 admit_low_medium 动作：组合不合法即拒绝（MC-1 边界侧）
        with pytest.raises(ValidationError):
            base_input(action=Action.ADMIT_LOW_MEDIUM)

    def test_missing_purpose_rejected(self) -> None:
        # purpose 缺失拒绝，不默认成 general（MC-12）
        values = base_input().model_dump(mode="python")
        del values["purpose"]
        with pytest.raises(ValidationError):
            PolicyInput(**values)

    def test_extra_fields_forbidden(self) -> None:
        # extra="forbid"：任何未声明字段（含 secret 形状）都不能进入 input/decision log
        with pytest.raises(ValidationError):
            PolicyInput(**{**base_input().model_dump(mode="python"), "access_token": "s3cr3t"})

    def test_secret_shaped_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            base_input(**{"credential": {"password": "s3cr3t"}})

    def test_policy_input_has_no_secret_shaped_fields(self) -> None:
        # input schema 本身不含任何 secret/token/password 形状字段（decision log 回显 input）
        names = set(PolicyInput.model_fields)
        assert names & {"secret", "token", "password", "credential"} == set()
        nested = {
            f for model in (Actor, ResourceRef, ResourceContext, Delegation, RequestContext)
            for f in model.model_fields
        }
        assert nested & {"secret", "token", "password", "credential"} == set()


class TestRoleBindingScope:
    def test_workspace_binding_requires_workspace_id(self) -> None:
        with pytest.raises(ValidationError):
            RoleBinding(name=Role.WORKSPACE_ADMIN, scope=RoleScope.WORKSPACE, organization_id=ORG)

    def test_org_binding_forbids_workspace_id(self) -> None:
        with pytest.raises(ValidationError):
            RoleBinding(name=Role.ORG_OWNER, scope=RoleScope.ORG, organization_id=ORG, workspace_id=WS)

    def test_workspace_context_requires_organization(self) -> None:
        # workspace_id 存在时 organization_id 必填（与 ActorContext 一致）
        with pytest.raises(ValidationError):
            base_input(organization_id=None)


class TestResourceContextRequirements:
    def test_review_publish_requires_last_content_author(self) -> None:
        # SoD 证据缺失的发布复核在边界拒绝，不能带着空证据进 Rego
        with pytest.raises(ValidationError):
            base_input(
                resource=ResourceRef(type=ResourceType.AGENT_PUBLISH, id=RES, version="v1"),
                action=Action.REVIEW_PUBLISH,
            )

    def test_review_publish_accepts_last_content_author(self) -> None:
        doc = base_input(
            resource=ResourceRef(type=ResourceType.AGENT_PUBLISH, id=RES, version="v1"),
            action=Action.REVIEW_PUBLISH,
            resource_context=ResourceContext(last_content_author_principal_id=RES),
        )
        assert doc.resource_context.last_content_author_principal_id == RES

    def test_approval_actions_require_requester(self) -> None:
        for action in (Action.APPROVE, Action.REJECT, Action.REPLACE):
            with pytest.raises(ValidationError):
                base_input(
                    resource=ResourceRef(type=ResourceType.TOOL_APPROVAL, id=RES, version="v1"),
                    action=action,
                )

    def test_approval_actions_accept_parties(self) -> None:
        doc = base_input(
            resource=ResourceRef(type=ResourceType.TOOL_APPROVAL, id=RES, version="v1"),
            action=Action.APPROVE,
            resource_context=ResourceContext(
                requester_principal_id=UUID("00000000-0000-0000-0000-00000000000b"),
                modifier_principal_ids=(UUID("00000000-0000-0000-0000-00000000000c"),),
            ),
        )
        assert doc.resource_context.requester_principal_id is not None

    def test_dual_control_requires_publisher_evidence(self) -> None:
        with pytest.raises(ValidationError):
            base_input(
                resource=ResourceRef(type=ResourceType.CAPABILITY_VERSION, id=RES, version="v1"),
                action=Action.REVIEW_HIGH_CRITICAL,
                risk=Risk.HIGH,
            )

    def test_dual_control_accepts_publisher_evidence(self) -> None:
        doc = base_input(
            resource=ResourceRef(type=ResourceType.CAPABILITY_VERSION, id=RES, version="v1"),
            action=Action.REVIEW_HIGH_CRITICAL,
            risk=Risk.HIGH,
            resource_context=ResourceContext(
                publisher_principal_id=UUID("00000000-0000-0000-0000-00000000000d"),
                publisher_roles=(Role.CAPABILITY_PUBLISHER,),
            ),
        )
        assert doc.resource_context.publisher_roles == (Role.CAPABILITY_PUBLISHER,)

    def test_own_actions_require_owner(self) -> None:
        for resource, action in (
            (ResourceType.ORG, Action.READ_SELF),
            (ResourceType.CONNECTION_SECRET, Action.REVOKE_OWN),
            (ResourceType.TEAM_MEMORY, Action.SUBMIT_OWN_CANDIDATE),
        ):
            with pytest.raises(ValidationError):
                base_input(resource=ResourceRef(type=resource, id=RES, version="v1"), action=action)


class TestDelegation:
    def test_delegation_scope_must_be_resource_dot_action(self) -> None:
        with pytest.raises(ValidationError):
            Delegation(
                granted_by_principal_id=UUID("00000000-0000-0000-0000-00000000000e"),
                scope="org.*",
                expires_at=NOW,
            )
        with pytest.raises(ValidationError):
            Delegation(
                granted_by_principal_id=UUID("00000000-0000-0000-0000-00000000000e"),
                scope="",
                expires_at=NOW,
            )

    def test_delegation_requires_expiry(self) -> None:
        # 无过期时间的委托不允许进入 input（time 维度由 context.now 判定）
        with pytest.raises(ValidationError):
            Delegation.model_validate({
                "granted_by_principal_id": "00000000-0000-0000-0000-00000000000e",
                "scope": "org.manage",
            })

    def test_valid_delegation_accepted(self) -> None:
        d = Delegation(
            granted_by_principal_id=UUID("00000000-0000-0000-0000-00000000000e"),
            scope="org.manage",
            expires_at=NOW,
        )
        assert d.scope == "org.manage"


class TestResourceBinding:
    def test_resource_id_required(self) -> None:
        # resource.id 缺失必须在边界拒绝（独立验收反例：缺失 id 的请求不得
        # 到达 OPA transport）
        with pytest.raises(ValidationError):
            ResourceRef(type=ResourceType.ORG, version="v1")
        doc = base_input().model_dump(mode="python")
        del doc["resource"]["id"]
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_resource_id_must_be_uuid(self) -> None:
        with pytest.raises(ValidationError):
            ResourceRef(type=ResourceType.ORG, id="r1", version="v1")

    @pytest.mark.parametrize("version", [None, ""])
    def test_resource_version_required_non_empty(self, version: str | None) -> None:
        with pytest.raises(ValidationError):
            ResourceRef(type=ResourceType.ORG, id=RES, version=version)
        doc = base_input().model_dump(mode="python")
        doc["resource"]["version"] = version
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)


class TestTimeAwareBoundary:
    def test_delegation_expires_at_must_be_timezone_aware(self) -> None:
        # naive datetime 在 Python 输入边界拒绝：Rego 按真实时刻比较，
        # naive 值无法表达时区语义
        with pytest.raises(ValidationError):
            Delegation(
                granted_by_principal_id=UUID("00000000-0000-0000-0000-00000000000e"),
                scope="org.manage",
                expires_at=datetime(2026, 8, 15, 1, 0, 0),
            )

    def test_request_context_now_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValidationError):
            base_input(context=RequestContext(now=datetime(2026, 8, 15)))

    def test_naive_datetime_via_dict_validation_rejected(self) -> None:
        # 经 dict 边界（enforcer/client 的真实输入路径）同样拒绝 naive
        doc = base_input().model_dump(mode="python")
        doc["context"]["now"] = datetime(2026, 8, 15)
        with pytest.raises(ValidationError):
            PolicyInput.model_validate(doc)

    def test_offset_datetime_accepted_and_preserved(self) -> None:
        # aware datetime 允许（Rego 端负责按真实时刻比较）
        d = Delegation(
            granted_by_principal_id=UUID("00000000-0000-0000-0000-00000000000e"),
            scope="org.manage",
            expires_at=datetime(2026, 8, 15, 1, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        )
        assert d.expires_at.utcoffset() == timedelta(hours=2)


class TestNestedExtraForbidden:
    def test_nested_secret_shaped_extras_rejected(self) -> None:
        # extra="forbid" 必须递归生效：actor/access_token、resource/credential、
        # context/password 及更深层嵌套的 secret 形状在边界拒绝
        sentinel = "s3cr3t"
        actor = base_input().model_dump(mode="python")["actor"]
        cases: list[dict] = [
            {"actor": {**actor, "access_token": sentinel}},
            {"resource": {"type": "org", "id": str(RES), "version": "v1", "credential": {"password": sentinel}}},
            {"context": {"now": NOW, "password": sentinel}},
            {"actor": {**actor, "roles": [{**actor["roles"][0], "api_key": sentinel}]}},
        ]
        for patch in cases:
            doc = base_input().model_dump(mode="python")
            for key, value in patch.items():
                doc[key] = value
            with pytest.raises(ValidationError):
                PolicyInput.model_validate(doc)


class TestEffectiveIdentity:
    def test_effective_identity_only_for_agents(self) -> None:
        # 有效身份是 agent 执行时背后的用户；user 直接执行不携带 effective_identity
        doc = base_input(
            actor=Actor(principal_id=RES, kind=PrincipalKind.AGENT_IDENTITY, roles=(ws_binding(),)),
            effective_identity=EffectiveIdentity(
                principal_id=UUID("00000000-0000-0000-0000-00000000000f"),
                kind=PrincipalKind.USER,
            ),
        )
        assert doc.effective_identity is not None
