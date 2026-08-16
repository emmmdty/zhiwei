"""S1 identity domain models：不依赖 FastAPI / SQLAlchemy / provider SDK。

事实源：DATA_MODEL §2、PERMISSIONS §1、总设计 §3.1 / §9.1、docs/API.md §1-2、
边界裁决 S1-T1 CONTRACT REPAIR。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhiwei.contracts.canonical import canonical_json
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


class ActorRoleBinding(_FrozenModel):
    """请求级 actor 的角色绑定（从已验证 membership 解析，供 PEP 构造 PolicyInput）。

    形状镜像 policy.input.RoleBinding 但独立声明：domain 层不得导入 policy 层（依赖方向
    见 docs/ARCHITECTURE.md §2），转换发生在 api.policy_gate。未知角色名在转换期按
    fail closed 拒绝，不在这里放宽。scope 取值 org | workspace，与 policy.roles.RoleScope
    的字符串值严格一致。
    """

    name: str = Field(min_length=1)
    scope: str
    organization_id: UUID
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _scope_workspace_consistency(self) -> ActorRoleBinding:
        if self.scope == "workspace" and self.workspace_id is None:
            raise ValueError("workspace-scoped binding requires workspace_id")
        if self.scope == "org" and self.workspace_id is not None:
            raise ValueError("org-scoped binding must not carry workspace_id")
        return self


class ActorContext(_FrozenModel):
    """请求级 actor 与显式租户作用域。

    首次登录的 authenticated Principal 可以没有 active Organization：organization_id 可空；
    workspace_id 非空时 organization_id 必须非空。S1-T1 没有 OIDC，routers 必须由调用方
    显式注入本依赖；T2 提供真实身份依赖。

    kind 默认 USER：S1 会话路径（OIDC 交互登录）只产生 USER principal，ServiceAccount /
    AgentIdentity 不创建会话；默认值只在 S1 会话路径内成立，AGENT 主体接入时改为显式必填。
    role_bindings 由 resolve_context 从已验证 memberships 填充，只含已解析 org/workspace 的
    绑定（repair addendum §3.2 provenance 裁决），绝不跨 org 携带。
    """

    principal_id: UUID
    organization_id: UUID | None = None
    workspace_id: UUID | None = None
    kind: PrincipalKind = PrincipalKind.USER
    role_bindings: tuple[ActorRoleBinding, ...] = ()

    @model_validator(mode="after")
    def _workspace_requires_organization(self) -> ActorContext:
        if self.workspace_id is not None and self.organization_id is None:
            raise ValueError("workspace_id requires organization_id")
        return self


# --------------------------------------------------------------------------- S1-T2 session / secret 领域契约
#
# 事实源：DATA_MODEL §2（AuthSession 为 principal/session 级、AAD 绑定
# session/issuer/subject/version、不绑定 Organization）、S1-T2 冻结交接。

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthSession(_FrozenModel):
    """principal/session 级交互会话；**不含** organization_id/workspace_id（DATA_MODEL §2）。

    - cookie 只保存 SHA-256 hash，不保存可直接重放的 cookie 值；
    - 所有 session 更新使用 expected_version CAS（version 单调递增）；
    - refresh 竞争 ownership 用有界 lease + 每次 ownership 唯一的 opaque owner token
      （DB 只存 SHA-256 hash）；refresh_state 三态：idle / leased / calling。
      leased→calling 与 calling→idle 的全部转换都以 owner token CAS 门禁（fencing）。
    """

    id: UUID
    cookie_token_hash: str = Field(min_length=64, max_length=64)
    principal_id: UUID
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    encrypted_token_ref: str = Field(min_length=1)
    csrf_hash: str = Field(min_length=64, max_length=64)
    expires_at: datetime
    idle_expires_at: datetime
    revoked_at: datetime | None = None
    version: int
    refresh_state: str = "idle"
    refresh_lease_expires_at: datetime | None = None
    refresh_owner_token_hash: str | None = Field(default=None, min_length=64, max_length=64)
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("cookie_token_hash", "csrf_hash", "refresh_owner_token_hash")
    @classmethod
    def _hash_is_sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_HEX_RE.fullmatch(value):
            raise ValueError("must be a lowercase sha256 hex digest")
        return value

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    @field_validator("refresh_state")
    @classmethod
    def _refresh_state_limited(cls, value: str) -> str:
        if value not in {"idle", "leased", "calling"}:
            raise ValueError("refresh_state must be idle, leased or calling")
        return value

    @model_validator(mode="after")
    def _revoke_clears_lease(self) -> AuthSession:
        if (
            self.revoked_at is not None
            and (
                self.refresh_state != "idle"
                or self.refresh_lease_expires_at is not None
                or self.refresh_owner_token_hash is not None
            )
        ):
            raise ValueError("revoked sessions must not hold a refresh lease or owner token")
        return self

    @model_validator(mode="after")
    def _owner_token_and_lease_consistent_with_state(self) -> AuthSession:
        """统一不变量（验收修订 5）：idle ⟺ owner/lease 皆 NULL；leased/calling ⟹ 皆非 NULL。

        fencing 前提：leased/calling 必须同时持有 owner token 与有界 lease；
        idle 残留 owner/lease 是非法持久化状态，不得投影成看似合法的 domain 对象。
        """
        if self.refresh_state == "idle":
            if (
                self.refresh_owner_token_hash is not None
                or self.refresh_lease_expires_at is not None
            ):
                raise ValueError("idle sessions must not hold a refresh owner token or lease")
        elif self.refresh_owner_token_hash is None or self.refresh_lease_expires_at is None:
            raise ValueError(
                "leased/calling sessions must hold a refresh owner token and lease"
            )
        return self

    @model_validator(mode="after")
    def _absolute_expiry_not_before_idle(self) -> AuthSession:
        if self.expires_at < self.idle_expires_at:
            raise ValueError("absolute expiry must not precede idle expiry")
        return self


class LoginAttempt(_FrozenModel):
    """短期 server-side OIDC login attempt（Authorization Code + PKCE S256）。

    state / nonce 只存 SHA-256 hash；code_verifier 必须保留原文——回调只带回 state，
    token exchange 需要 verifier，无法从请求参数恢复。
    """

    id: UUID
    state_hash: str = Field(min_length=64, max_length=64)
    nonce_hash: str = Field(min_length=64, max_length=64)
    code_verifier: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    schema_version: int = 1

    @field_validator("state_hash", "nonce_hash")
    @classmethod
    def _hash_is_sha256_hex(cls, value: str) -> str:
        if not _SHA256_HEX_RE.fullmatch(value):
            raise ValueError("must be a lowercase sha256 hex digest")
        return value

    @field_validator("schema_version")
    @classmethod
    def _schema_version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value

    @model_validator(mode="after")
    def _time_ordering(self) -> LoginAttempt:
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must not precede created_at")
        if self.consumed_at is not None and self.consumed_at < self.created_at:
            raise ValueError("consumed_at must not precede created_at")
        return self


class TokenEnvelopePayload(_FrozenModel):
    """加密保存的 OIDC token payload（明文只存在于受控解密返回值）。

    token kind 与版本绑定在 payload 内，防止密文混淆；repr 永不暴露 token 明文。
    """

    purpose: str = "oidc_session"
    token_kind: str = "access_refresh"
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    expires_at: datetime | None = None
    schema_version: int = 1

    @field_validator("token_kind")
    @classmethod
    def _token_kind_limited(cls, value: str) -> str:
        if value != "access_refresh":
            raise ValueError("token_kind must be access_refresh")
        return value

    @field_validator("schema_version")
    @classmethod
    def _schema_version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value


class TokenAAD(_FrozenModel):
    """token envelope 的 AAD 结构（S1-T2 冻结契约）。

    必须包含 purpose/session_id/issuer/subject/session_version/schema_version；
    明确**不得**包含 organization_id/workspace_id——首次登录无组织、同一 principal 可属
    多个组织，AAD 一旦绑定组织就无法在组织间移动会话。
    """

    purpose: str
    session_id: UUID
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    session_version: int
    schema_version: int = 1

    @field_validator("session_version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value

    def encode(self) -> bytes:
        """RFC 8785 canonical JSON 字节；同内容两次编码字节一致。"""
        return canonical_json(
            {
                "purpose": self.purpose,
                "session_id": str(self.session_id),
                "issuer": self.issuer,
                "subject": self.subject,
                "session_version": self.session_version,
                "schema_version": self.schema_version,
            }
        )
