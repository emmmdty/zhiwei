"""S1 identity domain models：不依赖 FastAPI / SQLAlchemy / provider SDK。

事实源：DATA_MODEL §2、PERMISSIONS §1、总设计 §3.1 / §9.1、docs/API.md §1-2、
边界裁决 S1-T1 CONTRACT REPAIR。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.contracts.time import ensure_utc


class IdentityCommandError(RuntimeError):
    """身份命令失败基类。"""


class PrincipalNotFoundError(IdentityCommandError):
    """目标 principal 不存在。"""


class PrincipalDisabledError(IdentityCommandError):
    """disabled principal 不能获得新的 membership。"""


class ExternalIdentityConflictError(IdentityCommandError):
    """(issuer, subject) 稳定键已绑定到另一个 principal。"""


class NameConflictError(IdentityCommandError):
    """Workspace/Organization 范围内资源名称已被占用。"""


class OrganizationExistsError(IdentityCommandError):
    """bootstrap 目标组织已存在且请求不是创建者的精确重放（租户接管拒绝）。"""


class ResourceConflictError(IdentityCommandError):
    """目标资源 id 已存在且请求不是原始幂等重放。"""


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    AGENT_IDENTITY = "agent_identity"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class _FrozenModel(BaseModel):
    """identity domain 基类：frozen + 未知字段拒绝（fail closed）。

    created_at validator 对不含该字段的子类（Membership / ActorContext 等）不生效，
    因此用 check_fields=False 允许在基类统一声明。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Principal(_FrozenModel):
    """跨 Organization 的身份主体；User / ServiceAccount / AgentIdentity 三种合法 kind。"""

    id: UUID
    kind: PrincipalKind
    status: PrincipalStatus = PrincipalStatus.ACTIVE
    schema_version: int = 1
    created_at: datetime

    @property
    def is_disabled(self) -> bool:
        return self.status is PrincipalStatus.DISABLED

    @property
    def supports_interactive_login(self) -> bool:
        """只有 User 走 OIDC 交互登录；ServiceAccount 是 workload 身份；AgentIdentity 永不登录。"""
        return self.kind is PrincipalKind.USER

    def disable(self) -> Principal:
        """返回 disabled 副本；已 disabled 时幂等返回自身。不可原地修改。"""
        if self.is_disabled:
            return self
        return self.model_copy(update={"status": PrincipalStatus.DISABLED})


class ExternalIdentity(_FrozenModel):
    """OIDC (issuer, subject) 稳定键。email 不是身份键：传入未知字段必须报错，不能静默忽略。"""

    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    principal_id: UUID

    @property
    def stable_key(self) -> tuple[str, str]:
        return (self.issuer, self.subject)


class Organization(_FrozenModel):
    """最高业务隔离边界（总设计 §3.1）；字段与 S0 organizations 表一致。"""

    id: UUID
    status: str = "active"
    policy_ref: str | None = None
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1
    created_at: datetime


class Workspace(_FrozenModel):
    """协作、资源、成本和策略边界；organization_id 必填（总设计 §3.1）。"""

    id: UUID
    organization_id: UUID
    name: str
    classification_ceiling: str = "PUBLIC"
    budget_policy: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1
    created_at: datetime


class Membership(_FrozenModel):
    """Organization 级角色绑定；不含 workspace 作用域。"""

    principal_id: UUID
    organization_id: UUID
    role_bindings: frozenset[str] = frozenset()


class WorkspaceMembership(_FrozenModel):
    """Workspace 级角色绑定；organization_id 与 workspace_id 必须同时存在。"""

    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID
    role_bindings: frozenset[str] = frozenset()


class Group(_FrozenModel):
    """Workspace 级分组（总设计 §3.1：Organization → Workspace → Group）。

    名称唯一范围是 Workspace：同一 Organization 的不同 Workspace 可存在同名 Group。
    """

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    name: str
    schema_version: int = 1
    created_at: datetime


class GroupMember(_FrozenModel):
    """Group 成员；(group_id, principal_id) 唯一，重试幂等，严格 Workspace scope。"""

    group_id: UUID
    organization_id: UUID
    workspace_id: UUID
    principal_id: UUID
    created_at: datetime


class ActorContext(_FrozenModel):
    """请求级 actor 与显式租户作用域。

    首次登录的 authenticated Principal 可以没有 active Organization：organization_id 可空；
    workspace_id 非空时 organization_id 必须非空。S1-T1 没有 OIDC，routers 必须由调用方
    显式注入本依赖；T2 提供真实身份依赖。
    """

    principal_id: UUID
    organization_id: UUID | None = None
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _workspace_requires_organization(self) -> ActorContext:
        if self.workspace_id is not None and self.organization_id is None:
            raise ValueError("workspace_id requires organization_id")
        return self
