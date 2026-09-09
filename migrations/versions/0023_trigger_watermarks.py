"""F-R5-05：trigger watermark/delivery nonce 持久层（P4 B4b）。

Revision ID: 0023_trigger_watermarks
Revises: 0022_ttl_sweep

背景：DiscoveryTriggerService 的 source_delta watermark 为进程内 dict——
重启即失、重复触发 run（F-R5-05）；webhook 校验仅 possession proof，无
nonce/时间窗。本迁移给 trigger 状态一个 tenant-scoped 持久面。

设计要点：

- scope/state_key 身份列不可变（唯一键 = (org, ws, scope, state_key)），
  仅 value/updated_at 列级 UPDATE 可变——watermark 推进 = UPSERT value，
  webhook nonce = INSERT ON CONFLICT DO NOTHING 一次性 claim；
- RLS ENABLE+FORCE + 租户策略与 0020 同款（org+ws GUC 双条件）；
- zhiwei_app：SELECT/INSERT 表级 + 列级 UPDATE（value/updated_at），
  无 DELETE（nonce 记录不可被应用角色抹除——重放防护的完整性依赖）；
- downgrade 逆序撤销。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_trigger_watermarks"
down_revision: str | None = "0022_ttl_sweep"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"
_TABLE = "trigger_watermarks"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
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
        sa.CheckConstraint("schema_version > 0", name="trigger_watermarks_schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_trigger_watermarks_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trigger_watermarks"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "scope",
            "state_key",
            name="uq_trigger_watermarks_tenant_scope_key",
        ),
    )
    op.create_index(
        "ix_trigger_watermarks_workspace",
        _TABLE,
        ["organization_id", "workspace_id"],
    )

    op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_TABLE}_tenant_isolation" ON "{_TABLE}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_TABLE}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_TABLE}" TO zhiwei_app')
    op.execute(f'GRANT UPDATE (value, updated_at) ON TABLE "{_TABLE}" TO zhiwei_app')


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS "{_TABLE}_tenant_isolation" ON "{_TABLE}"')
    op.drop_index("ix_trigger_watermarks_workspace", table_name=_TABLE)
    op.drop_table(_TABLE)
