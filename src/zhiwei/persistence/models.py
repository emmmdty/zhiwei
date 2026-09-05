"""S0 relational schema metadata.

The migration is deliberately self-contained; these models are runtime mappings, not migration input.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint("schema_version > 0", name="schema_version"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_ref: Mapped[str | None] = mapped_column(String(255))
    retention_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Principal(Base):
    """跨 Organization 的 identity-global 主体记录（不启用 RLS，不挂租户列）。"""

    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint("kind IN ('user', 'service_account', 'agent_identity')", name="kind"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint("schema_version > 0", name="schema_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExternalIdentity(Base):
    """OIDC (issuer, subject) 稳定键；主键即稳定键，禁止以 email 代替。"""

    __tablename__ = "external_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_external_identities_principal",
            ondelete="CASCADE",
        ),
    )

    issuer: Mapped[str] = mapped_column(String(2048), primary_key=True)
    subject: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthSession(Base):
    """principal/session 级交互会话（S1-T2）；不含 organization_id/workspace_id。

    cookie 只存 SHA-256 hash；所有更新走 expected_version CAS；refresh 用有界 lease。
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint(
            "refresh_state IN ('idle', 'leased', 'calling')", name="refresh_state"
        ),
        CheckConstraint("expires_at >= idle_expires_at", name="expiry_ordering"),
        CheckConstraint(
            "NOT (revoked_at IS NOT NULL AND "
            "(refresh_state <> 'idle' OR refresh_lease_expires_at IS NOT NULL "
            "OR refresh_owner_token_hash IS NOT NULL))",
            name="revoked_no_lease",
        ),
        # 统一不变量（与 0004 迁移一致）：idle ⟺ owner/lease 皆 NULL；
        # leased/calling ⟹ owner/lease 皆非 NULL
        CheckConstraint(
            "(refresh_state = 'idle' AND refresh_owner_token_hash IS NULL "
            "AND refresh_lease_expires_at IS NULL) OR "
            "(refresh_state IN ('leased', 'calling') "
            "AND refresh_owner_token_hash IS NOT NULL "
            "AND refresh_lease_expires_at IS NOT NULL)",
            name="owner_token_consistency",
        ),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_auth_sessions_principal",
            ondelete="CASCADE",
        ),
        UniqueConstraint("cookie_token_hash", name="uq_auth_sessions_cookie_token_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cookie_token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    principal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    issuer: Mapped[str] = mapped_column(String(2048), nullable=False)
    subject: Mapped[str] = mapped_column(String(1024), nullable=False)
    encrypted_token_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    refresh_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="idle"
    )
    refresh_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_owner_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class OidcLoginAttempt(Base):
    """短期 server-side OIDC login attempt；state/nonce 存 hash，verifier 短期保留。"""

    __tablename__ = "oidc_login_attempts"
    __table_args__ = (
        CheckConstraint("expires_at >= created_at", name="expiry"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        UniqueConstraint("state_hash", name="uq_oidc_login_attempts_state_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    state_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    issuer: Mapped[str] = mapped_column(String(2048), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class SecretEnvelope(Base):
    """AES-GCM envelope 行（S1-T2）；业务层只持 opaque ref，不接触 ciphertext 结构。

    version 是 expected_version CAS 计数器；key_version 是 KEK 版本；envelope_version
    是信封格式版本。PG 中绝无 token 明文。
    """

    __tablename__ = "secret_envelopes"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("envelope_version > 0", name="envelope_version"),
        CheckConstraint("key_version > 0", name="key_version"),
        CheckConstraint("octet_length(data_nonce) = 12", name="data_nonce_len"),
        CheckConstraint("octet_length(wrap_nonce) = 12", name="wrap_nonce_len"),
        CheckConstraint("octet_length(wrapped_dek) = 48", name="wrapped_dek_len"),
        CheckConstraint("purpose IN ('oidc_session')", name="purpose"),
        CheckConstraint("schema_version > 0", name="schema_version"),
    )

    ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    envelope_version: Mapped[int] = mapped_column(Integer, nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    data_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrap_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class Membership(Base):
    """Organization 级角色绑定；与 WorkspaceMembership 分离，role bindings 不跨作用域。"""

    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_memberships_principal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_memberships_organization",
            ondelete="CASCADE",
        ),
    )

    principal_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, index=True)
    role_bindings: Mapped[list[Any]] = mapped_column(
        JSON_VALUE, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrganizationBootstrapClaim(Base):
    """identity-global bootstrap claim（S1-T4 四轮）：一个 principal 最多一个 bootstrap org。

    不属于 tenant 数据面（0008）：无 RLS、无 org/ws 租户作用域语义，不给任何角色
    直接表权限——
    只允许经窄 SECURITY DEFINER 函数 zhiwei_claim_organization_bootstrap 访问
    （principal 级 advisory lock 串行化 + UNIQUE 第二层防线）。membership 生命周期
    不影响 claim：成员资格被删除不能重置 bootstrap 资格。
    """

    __tablename__ = "organization_bootstrap_claims"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_organization_bootstrap_claims_principal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_bootstrap_claims_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", name="uq_organization_bootstrap_claims_organization_id"
        ),
    )

    principal_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceMembership(Base):
    """Workspace 级角色绑定；organization_id 与 workspace_id 复合外键保证租户一致。"""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_workspace_memberships_principal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_workspace_memberships_workspace",
            ondelete="CASCADE",
        ),
    )

    principal_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, index=True)
    role_bindings: Mapped[list[Any]] = mapped_column(
        JSON_VALUE, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Group(Base):
    """Workspace 级用户分组（总设计 §3.1）；名称唯一范围是 Workspace，跨组织允许重名。"""

    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_groups_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "workspace_id", "id", name="uq_groups_scope_id"),
        UniqueConstraint(
            "organization_id", "workspace_id", "name", name="uq_groups_scope_name"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GroupMember(Base):
    """Group 的成员；(group_id, principal_id) 唯一，重试幂等，严格 Workspace scope。"""

    __tablename__ = "group_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "group_id"],
            ["groups.organization_id", "groups.workspace_id", "groups.id"],
            name="fk_group_members_group",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_group_members_principal",
            ondelete="CASCADE",
        ),
    )

    group_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    principal_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_workspaces_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id", name="uq_workspaces_org_id"),
        UniqueConstraint("organization_id", "name", name="uq_workspaces_org_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    classification_ceiling: Mapped[str] = mapped_column(String(32), default="PUBLIC")
    budget_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_agent_definitions_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_definitions_scope_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_definition_id"],
            [
                "agent_definitions.organization_id",
                "agent_definitions.workspace_id",
                "agent_definitions.id",
            ],
            name="fk_agent_versions_definition",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_versions_scope_id"
        ),
        UniqueConstraint(
            "agent_definition_id", "version", name="uq_agent_versions_definition_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_definition_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_runs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_version_id"],
            [
                "agent_versions.organization_id",
                "agent_versions.workspace_id",
                "agent_versions.id",
            ],
            name="fk_runs_agent_version",
        ),
        UniqueConstraint("organization_id", "workspace_id", "id", name="uq_runs_scope_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"
    __table_args__ = (
        CheckConstraint("sequence_no > 0", name="sequence"),
        CheckConstraint("payload_schema_version > 0", name="payload_schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_events_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "sequence_no", name="uq_canonical_events_run_sequence"),
        UniqueConstraint(
            "organization_id", "run_id", "idempotency_key", name="uq_events_idempotency"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    epoch_id: Mapped[UUID | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(String(71))
    event_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalProjection(Base):
    __tablename__ = "canonical_projections"
    __table_args__ = (
        CheckConstraint("sequence_no >= 0", name="sequence"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_projections_run",
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    head_event_digest: Mapped[str | None] = mapped_column(String(71))
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArtifactManifest(Base):
    __tablename__ = "artifact_manifests"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_artifact_manifests_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_artifact_manifests_scope_id"
        ),
        UniqueConstraint(
            "organization_id", "object_key", name="uq_artifact_manifests_org_object_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_schema_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    retention: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    encryption_key_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_dataset_versions_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_dataset_versions_manifest",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_dataset_versions_scope_id"
        ),
        UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_dataset_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalSuiteVersion(Base):
    __tablename__ = "eval_suite_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_suite_versions_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_eval_suite_versions_scope_id"
        ),
        UniqueConstraint("suite_id", "version", name="uq_eval_suite_versions_suite_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    suite_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_runs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_eval_runs_run",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_version_id"],
            [
                "dataset_versions.organization_id",
                "dataset_versions.workspace_id",
                "dataset_versions.id",
            ],
            name="fk_eval_runs_dataset_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_suite_version_id"],
            [
                "eval_suite_versions.organization_id",
                "eval_suite_versions.workspace_id",
                "eval_suite_versions.id",
            ],
            name="fk_eval_runs_suite_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "campaign_id"],
            [
                "eval_campaigns.organization_id",
                "eval_campaigns.workspace_id",
                "eval_campaigns.id",
            ],
            name="fk_eval_runs_campaign",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "prereg_manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_eval_runs_prereg_manifest",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "model_manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_eval_runs_model_manifest",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_eval_runs_source_manifest",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "attempt_manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_eval_runs_attempt_manifest",
        ),
        UniqueConstraint("organization_id", "workspace_id", "id", name="uq_eval_runs_scope_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    dataset_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    eval_suite_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    campaign_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    prereg_manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    model_manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt_manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalSample(Base):
    __tablename__ = "eval_samples"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_run_id"],
            ["eval_runs.organization_id", "eval_runs.workspace_id", "eval_runs.id"],
            name="fk_eval_samples_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "eval_run_id", "sample_id", "unit_id", name="uq_eval_samples_registered_unit"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    eval_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sample_id: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    result_digest: Mapped[str | None] = mapped_column(String(71))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalCampaign(Base):
    """S9 eval campaign：一个冻结 suite 注册单位的划分计划；子运行挂在 eval_runs.campaign_id。

    suite_id/version 是逻辑标识（与 eval_suite_versions 同构，不建 FK）；status 只由
    全部子运行 sealed 推导，completed 是终态。
    """

    __tablename__ = "eval_campaigns"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("unit_count >= 0", name="unit_count"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        CheckConstraint(
            "status IN ('running', 'partial', 'completed')", name="campaign_status"
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_campaigns_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_eval_campaigns_scope_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    suite_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_idempotency_records_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_idempotency_records_workspace",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_tenant_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        CheckConstraint("audit_schema_version IN (1, 2)", name="audit_schema_version"),
        CheckConstraint(
            "result IS NULL OR result IN ('allowed', 'denied', 'failed')", name="result"
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR ("
            "effective_identity_ref IS NOT NULL AND resource_version IS NOT NULL "
            "AND result IS NOT NULL AND request_id IS NOT NULL AND trace_id IS NOT NULL)",
            name="v2_complete",
        ),
        CheckConstraint(
            "audit_schema_version = 2 OR ("
            "effective_identity_ref IS NULL AND resource_version IS NULL "
            "AND decision_id IS NULL AND policy_revision IS NULL "
            "AND decision_reason IS NULL AND result IS NULL "
            "AND request_id IS NULL AND trace_id IS NULL)",
            name="v1_shape",
        ),
        # 0006 audit contract（repair addendum §3.1.6）：v2 行 metadata 配对与格式边界，
        # 与 AuditRecord/AuditEventData Pydantic 校验逐字一致（direct INSERT 不可绕过）。
        CheckConstraint(
            "audit_schema_version = 1 OR (decision_reason IS NOT NULL "
            "AND length(decision_reason) > 0)",
            name="v2_decision_reason",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR ((decision_id IS NULL) = (policy_revision IS NULL))",
            name="v2_decision_pairing",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR result <> 'allowed' OR "
            "(decision_id IS NOT NULL AND policy_revision IS NOT NULL)",
            name="v2_allowed_metadata",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR result <> 'failed' OR "
            "(decision_id IS NULL AND policy_revision IS NULL)",
            name="v2_failed_metadata",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR payload_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="v2_payload_digest",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR resource_version >= 0",
            name="v2_resource_version",
        ),
        # 0007 audit metadata non-empty（二轮修复）：v2 行的 decision_id/policy_revision
        # 同为 NULL 或同为长度 ≥ 1 的非空串；空串在 DB 层同样拒绝（direct INSERT 不可绕过）。
        CheckConstraint(
            "audit_schema_version = 1 OR (decision_id IS NULL OR length(decision_id) > 0)",
            name="v2_decision_id_nonempty",
        ),
        CheckConstraint(
            "audit_schema_version = 1 OR (policy_revision IS NULL OR length(policy_revision) > 0)",
            name="v2_policy_revision_nonempty",
        ),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_events_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_audit_events_workspace",
        ),
        UniqueConstraint("event_digest", name="uq_audit_events_event_digest"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "previous_event_digest",
            name="uq_audit_events_scope_previous",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    # 0005 结构化审计字段：v1 行为 NULL（旧 digest 契约逐字节不变）；v2 行由 CHECK 强制必填
    audit_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    effective_identity_ref: Mapped[str | None] = mapped_column(Text)
    resource_version: Mapped[int | None] = mapped_column(Integer)
    decision_id: Mapped[str | None] = mapped_column(Text)
    policy_revision: Mapped[str | None] = mapped_column(Text)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(String(71))
    event_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'dead_letter')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'processing' AND claimed_by IS NOT NULL "
            "AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claimed_by IS NULL "
            "AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL)",
            name="claim",
        ),
        CheckConstraint(
            "(status = 'dead_letter' AND dead_lettered_at IS NOT NULL) OR "
            "(status <> 'dead_letter' AND dead_lettered_at IS NULL)",
            name="dead_letter",
        ),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_outbox_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_outbox_workspace",
        ),
        Index("ix_outbox_dispatch", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalRequestRow(Base):
    """S2-T7 审批请求（0011）：跨账号可见的审批旅程持久层。"""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'revoked', 'expired')",
            name="approval_status",
        ),
        CheckConstraint(
            "(status IN ('approved', 'rejected')) = "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="approval_decision_fields",
        ),
        CheckConstraint(
            "decided_by IS NULL OR decided_by <> requester",
            name="approval_sod_requester",
        ),
        CheckConstraint("schema_version > 0", name="approval_schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_approval_requests_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_approval_requests_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "run_id",
            "task_id",
            "input_digest",
            name="uq_approval_requests_task_digest",
        ),
        Index("ix_approval_requests_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    requester: Mapped[str] = mapped_column(String(255), nullable=False)
    last_input_modifier: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decision_reason: Mapped[str | None] = mapped_column(Text())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[UUID | None] = mapped_column(Uuid)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryRecordRow(Base):
    """S7 memory 记录持久层（0012）：DATA_MODEL §6 MemoryRecord 的投影。

    status 状态机 candidate/confirmed/superseded/revoked/expired，不原地覆盖：
    内容列（scope/type/subject/key/canonical_value 等）无 UPDATE 授权且由终态
    触发器守护；可变更列仅为生命周期转移列与 ADR-009 证据合并列（source_refs/
    observed_at/confidence）。纠正创建新记录并把原记录置 superseded；
    candidate 去重由 partial unique 索引在数据面强制（队列收敛的最后一道防线）。
    """

    __tablename__ = "memory_records"
    __table_args__ = (
        CheckConstraint("scope IN ('user', 'team', 'case')", name="memory_scope"),
        CheckConstraint(
            "type IN ('preference', 'fact', 'decision', 'episode', 'lesson')",
            name="memory_type",
        ),
        CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high')", name="memory_sensitivity"
        ),
        CheckConstraint(
            "status IN ('candidate', 'confirmed', 'superseded', 'revoked', 'expired')",
            name="memory_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="memory_confidence"),
        CheckConstraint("version > 0", name="memory_version"),
        CheckConstraint("acl_version > 0", name="memory_acl_version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        CheckConstraint(
            "(status = 'superseded') = (superseded_by IS NOT NULL)",
            name="memory_superseded_pairing",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_memory_records_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "uq_memory_records_candidate_dedup",
            "organization_id",
            "workspace_id",
            "dedup_hash",
            unique=True,
            postgresql_where=text("status = 'candidate'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_value: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(
        JSON_VALUE, nullable=False, server_default=text("'[]'::jsonb")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    author_ref: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    approver_ref: Mapped[UUID | None] = mapped_column(Uuid)
    conflict_refs: Mapped[list[Any]] = mapped_column(
        JSON_VALUE, nullable=False, server_default=text("'[]'::jsonb")
    )
    retention_policy: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_profile_refs: Mapped[list[Any]] = mapped_column(
        JSON_VALUE, nullable=False, server_default=text("'[]'::jsonb")
    )
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    superseded_by: Mapped[UUID | None] = mapped_column(Uuid)
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    tombstone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    dedup_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemoryLifecycleEventRow(Base):
    """S7 memory 生命周期台账（0012）：记录级状态转移 + 审计的追加式持久层。

    Run 内的 candidate/refusal 走 canonical_events；Run 外的转移（steward
    confirm、supersede、TTL expire）落本表 + audit_events（同事务）。幂等键为
    (record_id, action, payload_digest)：一次性转移重放与同证据合并重试均不会
    产生第二行。
    """

    __tablename__ = "memory_lifecycle_events"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_memory_lifecycle_events_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["record_id"],
            ["memory_records.id"],
            name="fk_memory_lifecycle_events_record",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "record_id",
            "action",
            "payload_digest",
            name="uq_memory_lifecycle_events_transition",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str] = mapped_column(String(16), nullable=False)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CostReservationRow(Base):
    """S9 cost ledger 预订行（0014）：CostLedger.reserve 的持久层投影。

    只追加、不可变（无 UPDATE/DELETE 授权）：reserve 是一次性事实，纠正通过
    reconcile 的 variance 如实记录，不原地改写金额。金额用 NUMERIC(18,6)——
    浮点会让金额逐字节不可复算，破坏「账本可审计」。
    """

    __tablename__ = "cost_reservations"
    __table_args__ = (
        CheckConstraint("amount_usd >= 0", name="cost_reservation_amount"),
        CheckConstraint(
            "price_confidence IN ('exact', 'estimated')",
            name="cost_reservation_price_confidence",
        ),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_cost_reservations_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_cost_reservations_run",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price_source: Mapped[str] = mapped_column(String(255), nullable=False)
    price_confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CostReconciliationRow(Base):
    """S9 cost ledger 对账行（0014）：CostLedger.reconcile 的持久层投影。

    variance 允许为负（节省）与超限（ROI 指标不是门禁，ADR-002）；分项成本
    （retry/child/tool external）独立列存放，不并入主消耗口径。每个预订至多
    一条 reconcile（唯一约束 = 域层 double-reconcile 拒绝的数据面备份）。
    """

    __tablename__ = "cost_reconciliations"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_cost_reconciliations_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["reservation_id"],
            ["cost_reservations.id"],
            name="fk_cost_reconciliations_reservation",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "reservation_id",
            name="uq_cost_reconciliations_reservation",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    reservation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    actual_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    variance_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    retry_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    child_run_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    tool_external_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
