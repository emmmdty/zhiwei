"""S1-T3 策略输入：严格类型、规范化与协议边界。

本模块构造发送给 OPA 的规范化 input（s1 规格 §3：actor/effective identity、
resource/version、purpose、classification、risk、workspace、delegation、
request context）。职责边界：
- 未知枚举（角色/资源/动作/作用域/分级/风险/purpose）在边界拒绝（fail closed）；
- SoD 证据缺失（发布复核人、审批当事人、双控发布者）在边界拒绝，不带着空证据进 Rego；
- extra="forbid"：任何未声明字段（包括 secret 形状）都不能进入 input——
  OPA decision log 会回显 input（PERMISSIONS.md §13 secret 不进入 decision log）；
- 授权语义不在此层：角色→权限映射只在 authz.rego。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.identity.domain import PrincipalKind
from zhiwei.policy.roles import (
    RESOURCE_ACTIONS,
    Action,
    Classification,
    Purpose,
    ResourceType,
    Risk,
    Role,
    RoleScope,
    normalize_role,
)

_DELEGATION_SCOPE_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")


def _require_aware_datetime(value: datetime) -> datetime:
    """拒绝 naive datetime：Rego 按真实时刻比较，naive 无法表达时区语义。

    tzinfo 非 None 但 utcoffset 为 None 的时钟同样拒绝（假 tzinfo 无法提供
    真实时刻），保证比较基准是确定的 UTC 时刻。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class RoleBinding(BaseModel):
    """规范化角色绑定：矩阵的 org/workspace 作用域在这里显式化。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Role = Field(description="冻结角色名（normalize_role 后的规范值）")
    scope: RoleScope
    organization_id: UUID
    workspace_id: UUID | None = Field(default=None, validate_default=True)

    @field_validator("workspace_id")
    @classmethod
    def _workspace_id_consistency(cls, value: UUID | None, info: Any) -> UUID | None:
        scope = info.data.get("scope")
        if scope is RoleScope.WORKSPACE and value is None:
            raise ValueError("workspace-scoped binding requires workspace_id")
        if scope is RoleScope.ORG and value is not None:
            raise ValueError("org-scoped binding must not carry workspace_id")
        return value


class Actor(BaseModel):
    """执行主体；roles 是 PEP 从权威 memberships 解析出的绑定（不信任 caller）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: UUID
    kind: PrincipalKind
    roles: tuple[RoleBinding, ...] = ()
    # 二轮修复：bootstrap 判定输入（PEP 从权威 memberships 解析；None = 无 active org）
    active_organization_id: UUID | None = None


class EffectiveIdentity(BaseModel):
    """agent 执行时背后的有效主体（人类 principal）；user 直行时不携带。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: UUID
    kind: PrincipalKind


class ResourceRef(BaseModel):
    """动作目标：类型/标识/版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ResourceType
    # 独立验收反例：缺 id/空 version 的请求不得到达 OPA transport——Rego 按
    # id+version 绑定动作目标，缺失会让 own/SoD 判定失去目标
    id: UUID
    version: str = Field(min_length=1)


class ResourceContext(BaseModel):
    """SoD/own 判定所需资源事实（由 PEP 从权威记录解析，非 caller 自述）。

    每个字段只对特定 (resource, action) 生效（见 PolicyInput 的按类型校验）；
    其余动作允许为空。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_principal_id: UUID | None = None
    last_content_author_principal_id: UUID | None = None
    requester_principal_id: UUID | None = None
    modifier_principal_ids: tuple[UUID, ...] = ()
    agent_identity_principal_id: UUID | None = None
    publisher_principal_id: UUID | None = None
    publisher_roles: tuple[Role, ...] = ()


class Delegation(BaseModel):
    """委托链中的一跳（ADR-008 的链上 CAS/环检测在 S2 运行时，此处只管 scope/时效）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    granted_by_principal_id: UUID
    scope: str = Field(description="委托范围，格式 resource.action（无通配符）")
    expires_at: datetime = Field(description="委托必须有过期时间（time 维）")

    @field_validator("scope")
    @classmethod
    def _scope_format(cls, value: str) -> str:
        if not _DELEGATION_SCOPE_RE.match(value):
            raise ValueError("delegation scope must be resource.action without wildcards")
        return value

    @field_validator("expires_at")
    @classmethod
    def _expires_at_aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value)


class RequestContext(BaseModel):
    """请求上下文（server 侧推导）：now 参与时间维（委托过期），ceiling 来自 workspace policy。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    now: datetime

    @field_validator("now")
    @classmethod
    def _now_aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value)

    classification_ceiling: Classification | None = None
    requires_delegation: bool = False
    method: str | None = None
    ip: str | None = None
    trace_id: str | None = None


# SoD/own 动作对 ResourceContext 字段的强制要求：证据缺失在边界拒绝。
_REQUIRED_OWNER_ACTIONS: frozenset[tuple[ResourceType, Action]] = frozenset({
    (ResourceType.ORG, Action.READ_SELF),
    (ResourceType.CONNECTION_SECRET, Action.CREATE_OWN),
    (ResourceType.CONNECTION_SECRET, Action.REVOKE_OWN),
    (ResourceType.TEAM_MEMORY, Action.SUBMIT_OWN_CANDIDATE),
})
_REQUIRED_LAST_AUTHOR_ACTIONS: frozenset[tuple[ResourceType, Action]] = frozenset({
    (ResourceType.AGENT_PUBLISH, Action.REVIEW_PUBLISH),
})
_REQUIRED_APPROVAL_ACTIONS: frozenset[tuple[ResourceType, Action]] = frozenset({
    (ResourceType.TOOL_APPROVAL, Action.APPROVE),
    (ResourceType.TOOL_APPROVAL, Action.REJECT),
    (ResourceType.TOOL_APPROVAL, Action.REPLACE),
})
_REQUIRED_DUAL_CONTROL_ACTIONS: frozenset[tuple[ResourceType, Action]] = frozenset({
    (ResourceType.CAPABILITY_VERSION, Action.REVIEW_HIGH_CRITICAL),
})


class PolicyInput(BaseModel):
    """OPA input 的严格 schema。任何未声明字段被拒（secret 进不来）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: Actor
    effective_identity: EffectiveIdentity | None = None
    organization_id: UUID
    workspace_id: UUID | None = None
    resource: ResourceRef
    action: Action
    purpose: Purpose
    classification: Classification | None = None
    risk: Risk | None = None
    delegation: tuple[Delegation, ...] = ()
    resource_context: ResourceContext = ResourceContext()
    context: RequestContext

    @field_validator("action")
    @classmethod
    def _action_enum_member(cls, value: Action) -> Action:
        # 防止任意字符串绕过 StrEnum 校验（StrEnum 允许直接构造未知成员值）
        if not isinstance(value, Action) or value.value not in {a.value for a in Action}:
            raise ValueError(f"unknown action: {value!r}")
        return value

    @model_validator(mode="after")
    def _validate(self) -> Self:
        pair = (self.resource.type, self.action)
        if self.action not in RESOURCE_ACTIONS[self.resource.type]:
            raise ValueError(
                f"action {self.action.value!r} is not valid for resource "
                f"{self.resource.type.value!r}"
            )
        # agent 执行必须携带有效主体（PERMISSIONS.md:9-10 双身份记录）；缺失会让
        # Rego 的 via_effective SoD 规则全部失效，只能在边界拒绝
        if self.actor.kind is PrincipalKind.AGENT_IDENTITY and self.effective_identity is None:
            raise ValueError("agent_identity actor requires effective_identity")
        ctx = self.resource_context
        if pair in _REQUIRED_OWNER_ACTIONS and ctx.owner_principal_id is None:
            raise ValueError("own-scoped action requires owner_principal_id")
        if pair in _REQUIRED_LAST_AUTHOR_ACTIONS and ctx.last_content_author_principal_id is None:
            raise ValueError("review_publish requires last_content_author_principal_id")
        if pair in _REQUIRED_APPROVAL_ACTIONS and ctx.requester_principal_id is None:
            raise ValueError("approval action requires requester_principal_id")
        if pair in _REQUIRED_DUAL_CONTROL_ACTIONS and (
            ctx.publisher_principal_id is None or not ctx.publisher_roles
        ):
            raise ValueError("review_high_critical requires publisher evidence")
        return self


def binding_from_membership(
    role: str, *, scope: RoleScope, organization_id: UUID, workspace_id: UUID | None = None
) -> RoleBinding:
    """从 memberships 记录构造规范化绑定；未知角色名抛 ValueError（fail closed）。

    历史字符串（owner/builder）经 LEGACY_ROLE_ALIASES 规范化；别名之外的任何
    值都按未知角色拒绝——不静默映射、不当作 Member 降级。
    """
    return RoleBinding(
        name=normalize_role(role),
        scope=scope,
        organization_id=organization_id,
        workspace_id=workspace_id,
    )
