"""S1 identity domain models：不依赖 FastAPI / SQLAlchemy / provider SDK。

事实源：DATA_MODEL §2、PERMISSIONS §1、总设计 §9.1、边界裁决 S1-T1。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.time import ensure_utc


class IdentityCommandError(RuntimeError):
    """身份命令失败基类。"""


class PrincipalNotFoundError(IdentityCommandError):
    """目标 principal 不存在。"""


class PrincipalDisabledError(IdentityCommandError):
    """disabled principal 不能获得新的 membership。"""


class ExternalIdentityConflictError(IdentityCommandError):
    """(issuer, subject) 稳定键已绑定到另一个 principal。"""


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    AGENT_IDENTITY = "agent_identity"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Principal(BaseModel):
    """跨 Organization 的身份主体；User / ServiceAccount / AgentIdentity 三种合法 kind。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    kind: PrincipalKind
    status: PrincipalStatus = PrincipalStatus.ACTIVE
    schema_version: int = 1
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

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


class ExternalIdentity(BaseModel):
    """OIDC (issuer, subject) 稳定键。email 不是身份键：传入未知字段必须报错，不能静默忽略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    principal_id: UUID

    @property
    def stable_key(self) -> tuple[str, str]:
        return (self.issuer, self.subject)


class Membership(BaseModel):
    """Organization 级角色绑定；不含 workspace 作用域。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: UUID
    organization_id: UUID
    role_bindings: frozenset[str] = frozenset()


class WorkspaceMembership(BaseModel):
    """Workspace 级角色绑定；organization_id 与 workspace_id 必须同时存在。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID
    role_bindings: frozenset[str] = frozenset()


class Group(BaseModel):
    """Organization 级分组。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    organization_id: UUID
    name: str
    schema_version: int = 1
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class GroupMember(BaseModel):
    """Group 成员；(group_id, principal_id) 唯一，重试幂等。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: UUID
    organization_id: UUID
    principal_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ActorContext(BaseModel):
    """请求级 actor 与显式租户作用域。

    S1-T1 没有 OIDC，routers 必须由调用方显式注入本依赖；T2 提供真实身份依赖。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID | None = None
