"""Create the S1-T2 auth/session/secret schema and identity-global role separation.

Revision ID: 0003_auth_sessions
Revises: 0002_identity

identity-global 独立数据库角色（冻结裁决，方案 A）：
- zhiwei_app 撤销对 principals / external_identities 的直接访问（0002 授权回滚），
  也不得接触 auth_sessions / oidc_login_attempts / secret_envelopes；
- zhiwei_identity 只获得 identity-global/auth/secret 表的最小权限，不得直接访问任何
  tenant-owned 表（organizations / workspaces / memberships / workspace_memberships /
  groups / group_members）；
- 跨组织 membership 发现只能调用两个窄 SECURITY DEFINER 函数：
  * zhiwei_principal_snapshot(uuid)：供 zhiwei_app 查询 principal 最小字段
    （kind/status/schema_version/created_at），支撑 T1 disabled 双保险；
  * zhiwei_principal_memberships(uuid)：仅供 zhiwei_identity 查询已认证 principal 的
    组织/工作区摘要（scope='organization' | 'workspace'）。
- 函数固定 search_path、完全限定表名、无动态 SQL、REVOKE EXECUTE FROM PUBLIC、
  分别授予明确角色；不提供任意 SQL / 全表导出 / token 读取接口。

AuthSession / token 密文契约：
- cookie 只存 SHA-256 hash；refresh 用有界 lease + expected_version CAS；
- secret_envelopes 保存 AES-GCM envelope（ciphertext / data_nonce / wrapped_dek /
  wrap_nonce / key_id / key_version / envelope_version / version CAS）；
  PG 中绝不出现 access_token / refresh_token 明文。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_auth_sessions"
down_revision: str | None = "0002_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_FUNCTION = "zhiwei_principal_snapshot"
_MEMBERSHIP_FUNCTION = "zhiwei_principal_memberships"


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cookie_token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=2048), nullable=False),
        sa.Column("subject", sa.String(length=1024), nullable=False),
        sa.Column("encrypted_token_ref", sa.String(length=255), nullable=False),
        sa.Column("csrf_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "refresh_state",
            sa.String(length=16),
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
        sa.Column("refresh_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_auth_sessions_version")),
        sa.CheckConstraint(
            "refresh_state IN ('idle', 'refreshing')",
            name=op.f("ck_auth_sessions_refresh_state"),
        ),
        sa.CheckConstraint(
            "expires_at >= idle_expires_at", name=op.f("ck_auth_sessions_expiry_ordering")
        ),
        sa.CheckConstraint(
            "NOT (revoked_at IS NOT NULL AND "
            "(refresh_state <> 'idle' OR refresh_lease_expires_at IS NOT NULL))",
            name=op.f("ck_auth_sessions_revoked_no_lease"),
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_auth_sessions_schema_version")),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_auth_sessions_principal",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("cookie_token_hash", name="uq_auth_sessions_cookie_token_hash"),
    )
    op.create_index("ix_auth_sessions_principal_id", "auth_sessions", ["principal_id"])

    op.create_table(
        "oidc_login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("nonce_hash", sa.CHAR(length=64), nullable=False),
        # PKCE verifier 无法从回调参数恢复，必须短期保留原文供 token exchange 使用；
        # 行级过期 + 一次性消费保证其生命周期最短。
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("issuer", sa.String(length=2048), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("expires_at >= created_at", name=op.f("ck_oidc_login_attempts_expiry")),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_oidc_login_attempts_schema_version")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_oidc_login_attempts"),
        sa.UniqueConstraint("state_hash", name="uq_oidc_login_attempts_state_hash"),
    )

    op.create_table(
        "secret_envelopes",
        sa.Column("ref", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("envelope_version", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(length=128), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("data_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_secret_envelopes_version")),
        sa.CheckConstraint(
            "envelope_version > 0", name=op.f("ck_secret_envelopes_envelope_version")
        ),
        sa.CheckConstraint("key_version > 0", name=op.f("ck_secret_envelopes_key_version")),
        # AES-GCM 固定参数：96-bit nonce、32-byte DEK + 16-byte tag
        sa.CheckConstraint(
            "octet_length(data_nonce) = 12", name=op.f("ck_secret_envelopes_data_nonce_len")
        ),
        sa.CheckConstraint(
            "octet_length(wrap_nonce) = 12", name=op.f("ck_secret_envelopes_wrap_nonce_len")
        ),
        sa.CheckConstraint(
            "octet_length(wrapped_dek) = 48", name=op.f("ck_secret_envelopes_wrapped_dek_len")
        ),
        sa.CheckConstraint(
            "purpose IN ('oidc_session')", name=op.f("ck_secret_envelopes_purpose")
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_secret_envelopes_schema_version")
        ),
        sa.PrimaryKeyConstraint("ref", name="pk_secret_envelopes"),
    )

    _create_functions()
    _grant_identity_global_privileges()


def downgrade() -> None:
    for table in ("secret_envelopes", "oidc_login_attempts", "auth_sessions"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_identity')
        op.drop_table(table)
    op.execute(f"DROP FUNCTION IF EXISTS public.{_SNAPSHOT_FUNCTION}(uuid)")
    op.execute(f"DROP FUNCTION IF EXISTS public.{_MEMBERSHIP_FUNCTION}(uuid)")
    # 恢复 0002 原权限：zhiwei_app 重新获得 identity-global 表的直接访问
    op.execute('REVOKE ALL PRIVILEGES ON TABLE "principals" FROM zhiwei_identity')
    op.execute('REVOKE ALL PRIVILEGES ON TABLE "external_identities" FROM zhiwei_identity')
    op.execute('GRANT SELECT, INSERT ON TABLE "principals" TO zhiwei_app')
    op.execute('GRANT UPDATE (status) ON TABLE "principals" TO zhiwei_app')
    op.execute('GRANT SELECT, INSERT ON TABLE "external_identities" TO zhiwei_app')


def _grant_identity_global_privileges() -> None:
    """zhiwei_app 撤销 + zhiwei_identity 最小权限。

    新表上显式 REVOKE zhiwei_app：防止「ALTER DEFAULT PRIVILEGES 曾把全部新表授权给
    app role」的既有环境残留默认 ACL（纵深防御，不依赖初始化脚本的干净状态）。
    """
    op.execute('REVOKE ALL PRIVILEGES ON TABLE "principals" FROM zhiwei_app')
    op.execute('REVOKE ALL PRIVILEGES ON TABLE "external_identities" FROM zhiwei_app')
    for table in ("auth_sessions", "oidc_login_attempts", "secret_envelopes"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
    op.execute('GRANT SELECT, INSERT ON TABLE "principals" TO zhiwei_identity')
    op.execute('GRANT UPDATE (status) ON TABLE "principals" TO zhiwei_identity')
    op.execute('GRANT SELECT, INSERT ON TABLE "external_identities" TO zhiwei_identity')
    op.execute('GRANT SELECT, INSERT, UPDATE ON TABLE "auth_sessions" TO zhiwei_identity')
    op.execute('GRANT SELECT, INSERT, UPDATE ON TABLE "oidc_login_attempts" TO zhiwei_identity')
    op.execute('GRANT SELECT, INSERT, UPDATE ON TABLE "secret_envelopes" TO zhiwei_identity')


def _create_functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{_SNAPSHOT_FUNCTION}(p_principal uuid)
        RETURNS TABLE (
            id uuid,
            kind text,
            status text,
            schema_version integer,
            created_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT p.id, p.kind, p.status, p.schema_version, p.created_at
            FROM public.principals AS p
            WHERE p.id = p_principal
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{_MEMBERSHIP_FUNCTION}(p_principal uuid)
        RETURNS TABLE (
            scope text,
            organization_id uuid,
            workspace_id uuid,
            role_bindings jsonb,
            organization_status text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT 'organization'::text AS scope,
                   m.organization_id,
                   NULL::uuid AS workspace_id,
                   m.role_bindings,
                   o.status AS organization_status
            FROM public.memberships AS m
            JOIN public.organizations AS o ON o.id = m.organization_id
            WHERE m.principal_id = p_principal
            UNION ALL
            SELECT 'workspace'::text AS scope,
                   wm.organization_id,
                   wm.workspace_id,
                   wm.role_bindings,
                   o.status AS organization_status
            FROM public.workspace_memberships AS wm
            JOIN public.organizations AS o ON o.id = wm.organization_id
            WHERE wm.principal_id = p_principal
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{_SNAPSHOT_FUNCTION}(uuid) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_MEMBERSHIP_FUNCTION}(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{_SNAPSHOT_FUNCTION}(uuid) TO zhiwei_app")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{_MEMBERSHIP_FUNCTION}(uuid) TO zhiwei_identity")
