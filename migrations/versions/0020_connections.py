"""F-R6-03：connections 持久层——Connection API 脱离进程内单例（P2b）。

Revision ID: 0020_connections
Revises: 0019_run_template

背景：api/connections.py 以模块级 dict 单例持有连接（S4 stub 形态），无 PEP/
无审计/重启即失。P2b 接线（F-R6-03）将其落 PG：tenant-scoped（org+ws 复合
FK → workspaces）、status 生命周期列可变、provider_version_id 为 capability
目录引用（capability 持久层未建，建 FK 反而指向不存在的表——存在性校验在
API 层经 capability 目录注入查询，不设外键）。

设计要点：

- subject_mode/status 用 CHECK 约束钉词表（域 enum 镜像，ADR-012 决策口径）；
- RLS ENABLE+FORCE + 租户策略与 0012 memory 同款（org+ws GUC 双条件）；
- zhiwei_app：SELECT/INSERT 表级 + 列级 UPDATE（status/version/updated_at，
  生命周期转移三列）——0012 最小授权惯例（test_database MUTABLE_COLUMNS 契约）；
  无 DELETE（revoke 是状态转移不是物理删行）；
- downgrade 逆序撤销。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_connections"
down_revision: str | None = "0019_run_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"
_TABLE = "connections"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_mode IN ('user_delegated', 'workspace_service', 'service_account')",
            name="connections_subject_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')",
            name="connections_status",
        ),
        sa.CheckConstraint("version > 0", name="connections_version"),
        sa.CheckConstraint("schema_version > 0", name="connections_schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_connections_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
    )
    op.create_index(
        "ix_connections_workspace", _TABLE, ["organization_id", "workspace_id"]
    )
    op.create_index("ix_connections_principal", _TABLE, ["principal_id"])

    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_TABLE}_tenant_isolation" ON "{_TABLE}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_TABLE}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_TABLE}" TO zhiwei_app')
    op.execute(
        f'GRANT UPDATE (status, version, updated_at) ON TABLE "{_TABLE}" TO zhiwei_app'
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS "{_TABLE}_tenant_isolation" ON "{_TABLE}"')
    op.drop_index("ix_connections_principal", table_name=_TABLE)
    op.drop_index("ix_connections_workspace", table_name=_TABLE)
    op.drop_table(_TABLE)
