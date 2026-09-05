"""S7：memory 生命周期持久化——memory_records + memory_lifecycle_events（FORCE RLS）。

Revision ID: 0012_memory
Revises: 0011_approval_requests

S7 plan Task 1/2：memory 域从内存态服务升级为 PG 持久层（plan 文件写作
0007_memory.py；迁移链按实际 head 顺延为 0012）。设计要点：

- memory_records 属 tenant 数据面：organization_id + workspace_id 复合外键、
  FORCE RLS（org+ws GUC 过滤，与 0001 的 _WORKSPACE_TABLES 同策略）；
- zhiwei_app 获 SELECT/INSERT + **列级 UPDATE**（生命周期转移列 + ADR-009
  证据合并列；表级 UPDATE 维持 S0 冻结契约「租户表无表级 UPDATE」，内容列
  不可变——状态机不原地覆盖，纠正创建 superseding version）；DELETE 不授
  （撤销/删除写 tombstone，不物理删除）；
- candidate 去重键（ADR-009）以 partial unique 索引在数据面强制：同租户内
  同时至多一条同 dedup 键的 candidate（队列收敛的最后防线）；
- memory_lifecycle_events 是 Run 外转移（confirm/supersede/expire/revoke）的
  追加式台账：Run 内 candidate/refusal 走 canonical_events，Run 外走本表 +
  audit_events（应用层同事务落账）；幂等键 (record_id, action, payload_digest)；
- 终态不可改写（纵深防御：应用层 status CAS 之外的数据面守护）；
- downgrade 撤销全部授权与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0012_memory"
down_revision: str | None = "0011_approval_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_RECORDS = "memory_records"
_EVENTS = "memory_lifecycle_events"

_RECORDS_UPDATE_COLUMNS = (
    "status",
    "updated_at",
    "approver_ref",
    "superseded_by",
    "revoked_reason",
    "tombstone",
    "confidence",
    "observed_at",
    "source_refs",
)


def upgrade() -> None:
    op.create_table(
        _RECORDS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_subject_id", pg.UUID(), nullable=False, index=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("canonical_value", sa.Text(), nullable=False),
        sa.Column(
            "source_refs",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("author_ref", pg.UUID(), nullable=False),
        sa.Column("approver_ref", pg.UUID(), nullable=True),
        sa.Column(
            "conflict_refs",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("retention_policy", sa.String(length=64), nullable=False),
        sa.Column(
            "allowed_profile_refs",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("acl_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("superseded_by", pg.UUID(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column(
            "tombstone", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("dedup_hash", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("scope IN ('user', 'team', 'case')", name="memory_scope"),
        sa.CheckConstraint(
            "type IN ('preference', 'fact', 'decision', 'episode', 'lesson')",
            name="memory_type",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high')", name="memory_sensitivity"
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'confirmed', 'superseded', 'revoked', 'expired')",
            name="memory_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="memory_confidence"
        ),
        sa.CheckConstraint("version > 0", name="memory_version"),
        sa.CheckConstraint("acl_version > 0", name="memory_acl_version"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_by IS NOT NULL)",
            name="memory_superseded_pairing",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_memory_records_workspace",
            ondelete="CASCADE",
        ),
    )
    # ADR-009 candidate 去重：同租户内同 dedup 键至多一条活跃 candidate。
    # superseded/revoked/expired 与新版本并存（时态共存），故 partial。
    op.create_index(
        "uq_memory_records_candidate_dedup",
        _RECORDS,
        ["organization_id", "workspace_id", "dedup_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'candidate'"),
    )

    op.create_table(
        _EVENTS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("record_id", pg.UUID(), nullable=False, index=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=False),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_memory_lifecycle_events_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["memory_records.id"],
            name="fk_memory_lifecycle_events_record",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "record_id",
            "action",
            "payload_digest",
            name="uq_memory_lifecycle_events_transition",
        ),
    )

    # 终态不可改写 + 内容列不可变（纵深防御：应用层 status CAS 之外的数据面守护；
    # 内容列本就无 UPDATE 授权，触发器挡住 migrator/未来授权漂移）。
    op.execute(
        """
        CREATE FUNCTION memory_records_guard() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.status IN ('superseded', 'revoked', 'expired') THEN
                RAISE EXCEPTION 'memory record % is terminal (%)', OLD.id, OLD.status;
            END IF;
            IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.scope_subject_id IS DISTINCT FROM OLD.scope_subject_id
               OR NEW.type IS DISTINCT FROM OLD.type
               OR NEW.subject IS DISTINCT FROM OLD.subject
               OR NEW.key IS DISTINCT FROM OLD.key
               OR NEW.canonical_value IS DISTINCT FROM OLD.canonical_value
               OR NEW.author_ref IS DISTINCT FROM OLD.author_ref
               OR NEW.dedup_hash IS DISTINCT FROM OLD.dedup_hash
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.version IS DISTINCT FROM OLD.version
               OR NEW.acl_version IS DISTINCT FROM OLD.acl_version THEN
                RAISE EXCEPTION 'memory record % content is immutable', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f'CREATE TRIGGER memory_records_lifecycle_guard BEFORE UPDATE ON "{_RECORDS}" '
        "FOR EACH ROW EXECUTE FUNCTION memory_records_guard()"
    )

    op.execute(f'ALTER TABLE "{_RECORDS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_RECORDS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_RECORDS}_tenant_isolation" ON "{_RECORDS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RECORDS}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_RECORDS}" TO zhiwei_app')
    # 列级 UPDATE：生命周期转移（status/approver/superseded_by/revoked_reason/
    # tombstone/updated_at）+ ADR-009 证据合并（source_refs/observed_at/confidence）；
    # 表级 UPDATE 维持冻结契约不授
    op.execute(
        f'GRANT UPDATE ({", ".join(_RECORDS_UPDATE_COLUMNS)}) ON TABLE "{_RECORDS}" '
        "TO zhiwei_app"
    )

    op.execute(f'ALTER TABLE "{_EVENTS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_EVENTS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_EVENTS}_tenant_isolation" ON "{_EVENTS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_EVENTS}" FROM PUBLIC')
    # 台账只追加：SELECT/INSERT 即完整能力，无 UPDATE/DELETE
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_EVENTS}" TO zhiwei_app')


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_records_lifecycle_guard ON memory_records")
    op.execute("DROP FUNCTION IF EXISTS memory_records_guard()")
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_EVENTS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_EVENTS}_tenant_isolation" ON "{_EVENTS}"')
    op.execute(f'ALTER TABLE "{_EVENTS}" DISABLE ROW LEVEL SECURITY')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RECORDS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_RECORDS}_tenant_isolation" ON "{_RECORDS}"')
    op.execute(f'ALTER TABLE "{_RECORDS}" DISABLE ROW LEVEL SECURITY')
    op.drop_table(_EVENTS)
    op.drop_index("uq_memory_records_candidate_dedup", table_name=_RECORDS)
    op.drop_table(_RECORDS)
