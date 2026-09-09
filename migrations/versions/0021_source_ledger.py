"""F-R6-04：Source Ledger 持久层——source_objects / source_versions（P2b）。

Revision ID: 0021_source_ledger
Revises: 0020_connections

背景：knowledge/ledger.py 自述 "In production, this is backed by PostgreSQL +
ObjectStore"，实际生产只有进程内字典（F-R6-04）；SyncManager 的 DELETE/REVOKE
SyncIntent 无 ledger 消费方——「delete/revoke 优先」在源事件到达后可能不生效。

设计要点（digest+immutable 协议，镜像 0012 memory 的守护模式）：

- source_objects：id/register 幂等（应用层保证）；可变列 = acl（当前 ACL
  权威，ADR-006 失权投影复检依赖）+ 运营生命周期列（lifecycle_status/
  last_sync_error——disable/connect/sync-error 运营面，与内容事实分离）；
  其余列不可变（触发器 + 列级 UPDATE 授权双层）；
- source_versions：state/tombstone/updated_at 之外全部不可变（更新 = 新版本，
  spec §3「Updates create new version」）；content_digest CHECK 钉 sha256
  协议（71 字符，contracts.py 同款校验的数据面镜像）；acl/classification
  存创建期解析值（域契约 non-optional，缺省继承在 create_version 落库前完成）；
- 唯一索引：(org, ws, object, version_seq) 与 (org, ws, object, content_digest)
  ——重复 digest 拒绝（DuplicateVersionError）的数据面防线；
- RLS FORCE + 租户策略与 0020 同款；zhiwei_app：SELECT/INSERT + 列级 UPDATE，
  无 DELETE（tombstone 语义，spec §3 不物理删行）；
- downgrade 逆序撤销。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_source_ledger"
down_revision: str | None = "0020_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"
_OBJECTS = "source_objects"
_VERSIONS = "source_versions"


def upgrade() -> None:
    op.create_table(
        _OBJECTS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("acl", postgresql.JSONB(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        # 运营生命周期（disable/connect/sync error）——与版本状态机（state）
        # 分离：disable 是操作暂停 + 级联失权（F-R6-04），不是内容事实变更
        sa.Column(
            "lifecycle_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
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
            "classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="source_objects_classification",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'disabled', 'error')",
            name="source_objects_lifecycle_status",
        ),
        sa.CheckConstraint("schema_version > 0", name="source_objects_schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_source_objects_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_objects"),
        # 复合 FK（source_versions → org+ws+object id）的被引用键
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "id",
            name="uq_source_objects_org_ws_id",
        ),
    )
    op.create_index(
        "ix_source_objects_workspace", _OBJECTS, ["organization_id", "workspace_id"]
    )

    # acl 是唯一可变列（当前 ACL 权威，ADR-006 失权投影复检依赖）；其余列不可变
    op.execute(
        """
        CREATE FUNCTION source_objects_guard() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.source_type IS DISTINCT FROM OLD.source_type
               OR NEW.classification IS DISTINCT FROM OLD.classification
               OR NEW.metadata IS DISTINCT FROM OLD.metadata
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'source object % content is immutable', OLD.id;
            -- 可变列：acl/updated_at（当前 ACL 权威）+ lifecycle_status/
            -- last_sync_error（运营生命周期）；内容列不可变
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f'CREATE TRIGGER source_objects_immutability_guard BEFORE UPDATE ON "{_OBJECTS}" '
        "FOR EACH ROW EXECUTE FUNCTION source_objects_guard()"
    )

    op.create_table(
        _VERSIONS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acl", postgresql.JSONB(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tombstone", sa.Boolean(), nullable=False),
        sa.Column("connector_version", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
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
            "state IN ('active', 'stale', 'revoked')", name="source_versions_state"
        ),
        sa.CheckConstraint(
            "classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')",
            name="source_versions_classification",
        ),
        sa.CheckConstraint(
            "content_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="source_versions_content_digest_format",
        ),
        sa.CheckConstraint("version_seq >= 1", name="source_versions_seq"),
        sa.CheckConstraint("schema_version > 0", name="source_versions_schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_source_versions_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_object_id"],
            ["source_objects.organization_id", "source_objects.workspace_id", "source_objects.id"],
            name="fk_source_versions_object",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_versions"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_object_id",
            "version_seq",
            name="uq_source_versions_seq",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_object_id",
            "content_digest",
            name="uq_source_versions_digest",
        ),
    )
    op.create_index(
        "ix_source_versions_workspace", _VERSIONS, ["organization_id", "workspace_id"]
    )

    # 内容不可变（更新 = 新版本）；生命周期列（state/tombstone/updated_at）
    # 可变——触发器 + 列级授权双层（0012 memory 同款）
    op.execute(
        """
        CREATE FUNCTION source_versions_guard() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.source_object_id IS DISTINCT FROM OLD.source_object_id
               OR NEW.version_seq IS DISTINCT FROM OLD.version_seq
               OR NEW.locator IS DISTINCT FROM OLD.locator
               OR NEW.content_digest IS DISTINCT FROM OLD.content_digest
               OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
               OR NEW.valid_at IS DISTINCT FROM OLD.valid_at
               OR NEW.acl IS DISTINCT FROM OLD.acl
               OR NEW.classification IS DISTINCT FROM OLD.classification
               OR NEW.parent_version_id IS DISTINCT FROM OLD.parent_version_id
               OR NEW.connector_version IS DISTINCT FROM OLD.connector_version
               OR NEW.parser_version IS DISTINCT FROM OLD.parser_version
               OR NEW.index_version IS DISTINCT FROM OLD.index_version
               OR NEW.metadata IS DISTINCT FROM OLD.metadata
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'source version % content is immutable', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f'CREATE TRIGGER source_versions_immutability_guard BEFORE UPDATE ON "{_VERSIONS}" '
        "FOR EACH ROW EXECUTE FUNCTION source_versions_guard()"
    )

    for table in (_OBJECTS, _VERSIONS):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
            f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
        )
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')

    # 列级 UPDATE：对象 acl（当前 ACL 权威）+ 运营生命周期列；版本生命周期列
    op.execute(
        f'GRANT UPDATE (acl, lifecycle_status, last_sync_error, updated_at) '
        f'ON TABLE "{_OBJECTS}" TO zhiwei_app'
    )
    op.execute(
        f'GRANT UPDATE (state, tombstone, updated_at) ON TABLE "{_VERSIONS}" TO zhiwei_app'
    )


def downgrade() -> None:
    op.execute('DROP FUNCTION IF EXISTS source_versions_guard() CASCADE')
    op.execute('DROP FUNCTION IF EXISTS source_objects_guard() CASCADE')
    op.drop_index("ix_source_versions_workspace", table_name=_VERSIONS)
    op.drop_table(_VERSIONS)
    op.drop_index("ix_source_objects_workspace", table_name=_OBJECTS)
    op.drop_table(_OBJECTS)
