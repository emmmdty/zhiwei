"""S10-T4b：Case surface 持久化——cases + case_events（FORCE RLS）。

Revision ID: 0017_cases
Revises: 0016_studio_agents

S6 收口补齐（specs/s6-evidence-ask.md §4、handoff s6-ask-evidence-e2e-exception
解锁条件）：S6-T3 交付的 Case 聚合只有 InMemory 仓储（交付报告登记的持久化缺口），
本迁移补上 PG 持久面。设计要点：

- cases 属 tenant 数据面：organization_id + workspace_id 复合外键、FORCE RLS
  （org+ws GUC 过滤，与 0001 的 _WORKSPACE_TABLES 同策略）；
- run 溯源：origin_run_id 复合外键到 runs（可空——Case 聚合本身不绑定 run，
  仅「从 run 创建」的 API 路径写入）；run 删除级联清溯溯源（不复制 transcript）；
- answer/evidence 关联以 JSONB id 列表存储（与 S6-T3 域模型 tuple-of-UUID 语义
  一致：Case 引用答案/证据的 id，不复制正文，无行级关联表）；
- zhiwei_app 获 SELECT/INSERT，无 UPDATE/DELETE：本任务只暴露创建与读取；
  生命周期转移（S6 §4.1 冻结状态机）未接 API，落地前不授予部分列更新；
- case_events 是追加式生命周期台账（0012 memory_lifecycle_events 同型）：
  commands.create_case 约定「caller 负责持久化 canonical 生命周期事件」——
  台账以 (case_id, event_type, payload_digest) 唯一键幂等，只有 SELECT/INSERT；
- CHECK 约束与 ORM 镜像（S9-T1 教训：迁移与 ORM 漂移会让 autogenerate 复现约束）；
- downgrade 撤销全部授权与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0017_cases"
down_revision: str | None = "0016_studio_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_CASES = "cases"
_EVENTS = "case_events"


def upgrade() -> None:
    op.create_table(
        _CASES,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("origin_run_id", pg.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "answer_ids",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_bundle_ids",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by", pg.UUID(), nullable=False),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(pg.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.CheckConstraint(
            "status IN ('created', 'active', 'triaged', 'open', 'resolved', 'archived')",
            name="case_status",
        ),
        sa.CheckConstraint("length(title) >= 1", name="case_title"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_cases_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "origin_run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_cases_origin_run",
            ondelete="CASCADE",
        ),
        # 复合外键（case_events → cases、cases.origin_run_id → runs）的引用目标
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_cases_scope_id"
        ),
    )
    op.create_index(
        "ix_cases_origin_run", _CASES, ["organization_id", "workspace_id", "origin_run_id"]
    )

    op.create_table(
        _EVENTS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("case_id", pg.UUID(), nullable=False, index=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
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
            name="fk_case_events_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "case_id"],
            ["cases.organization_id", "cases.workspace_id", "cases.id"],
            name="fk_case_events_case",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "case_id",
            "event_type",
            "payload_digest",
            name="uq_case_events_transition",
        ),
    )

    op.execute(f'ALTER TABLE "{_CASES}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_CASES}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_CASES}_tenant_isolation" ON "{_CASES}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CASES}" FROM PUBLIC')
    # 创建 + 读取即本任务的完整能力面；UPDATE/DELETE 不授（生命周期转移未接 API）
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_CASES}" TO zhiwei_app')

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
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_EVENTS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_EVENTS}_tenant_isolation" ON "{_EVENTS}"')
    op.execute(f'ALTER TABLE "{_EVENTS}" DISABLE ROW LEVEL SECURITY')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CASES}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_CASES}_tenant_isolation" ON "{_CASES}"')
    op.execute(f'ALTER TABLE "{_CASES}" DISABLE ROW LEVEL SECURITY')
    op.drop_table(_EVENTS)
    op.drop_index("ix_cases_origin_run", table_name=_CASES)
    op.drop_table(_CASES)
