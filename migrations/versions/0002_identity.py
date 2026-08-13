"""Create the S1 identity schema: principals, external identities, memberships and groups.

Revision ID: 0002_identity
Revises: 0001_foundation

租户边界（与 0001 的 GUC 约定一致）：
- Principal / ExternalIdentity 是跨 Organization 的 identity-global 记录，不启用 RLS；
- memberships / workspace_memberships / groups / group_members 是 tenant-owned：
  memberships、groups、group_members 按 organization_id 隔离，workspace_memberships 按
  organization_id + workspace_id 隔离；全部 FORCE RLS。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_identity"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_TENANT_RLS_POLICIES = {
    "memberships": f"organization_id = {_ORG_GUC}",
    "workspace_memberships": (
        f"organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}"
    ),
    "groups": f"organization_id = {_ORG_GUC}",
    "group_members": f"organization_id = {_ORG_GUC}",
}
_DROP_ORDER = (
    "group_members",
    "groups",
    "workspace_memberships",
    "memberships",
    "external_identities",
    "principals",
)


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('user', 'service_account', 'agent_identity')",
            name=op.f("ck_principals_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_principals_status")
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_principals_schema_version")),
        sa.PrimaryKeyConstraint("id", name="pk_principals"),
    )
    op.create_table(
        "external_identities",
        sa.Column("issuer", sa.String(length=2048), nullable=False),
        sa.Column("subject", sa.String(length=1024), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_external_identities_principal",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("issuer", "subject", name="pk_external_identities"),
    )
    op.create_index(
        "ix_external_identities_principal_id", "external_identities", ["principal_id"]
    )

    op.create_table(
        "memberships",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_memberships_principal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_memberships_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("principal_id", "organization_id", name="pk_memberships"),
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])

    op.create_table(
        "workspace_memberships",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role_bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_workspace_memberships_principal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_workspace_memberships_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("principal_id", "workspace_id", name="pk_workspace_memberships"),
    )
    op.create_index(
        "ix_workspace_memberships_organization_id",
        "workspace_memberships",
        ["organization_id"],
    )
    op.create_index("ix_workspace_memberships_workspace_id", "workspace_memberships", ["workspace_id"])

    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_groups_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_groups_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
        sa.UniqueConstraint("organization_id", "id", name="uq_groups_org_id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_groups_org_name"),
    )
    op.create_index("ix_groups_organization_id", "groups", ["organization_id"])

    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "group_id"],
            ["groups.organization_id", "groups.id"],
            name="fk_group_members_group",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_group_members_principal",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "principal_id", name="pk_group_members"),
    )
    op.create_index("ix_group_members_organization_id", "group_members", ["organization_id"])

    for table in ("principals", "external_identities"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')
    # disable 生命周期需要最小列级 UPDATE；delete 不在 T1 语义内
    op.execute('GRANT UPDATE (status) ON TABLE "principals" TO zhiwei_app')
    for table in ("memberships", "workspace_memberships"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
        op.execute(f'GRANT SELECT, INSERT, DELETE ON TABLE "{table}" TO zhiwei_app')
    for table in ("groups", "group_members"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')

    for table, expression in _TENANT_RLS_POLICIES.items():
        _enable_rls(table, expression)


def downgrade() -> None:
    for table in _DROP_ORDER:
        op.drop_table(table)


def _enable_rls(table: str, expression: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
        f"USING ({expression}) WITH CHECK ({expression})"
    )
