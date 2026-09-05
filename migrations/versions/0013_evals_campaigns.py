"""S9：eval campaign 持久化与 EvalRun 冻结引用列——eval_campaigns + eval_runs 扩展（FORCE RLS）。

Revision ID: 0013_evals_campaigns
Revises: 0012_memory

S9-T1：campaign 把冻结 suite 的注册单位划分到既有 EvalRun 运行时的子运行上，
不新建第二套 EvalRun 表。设计要点：

- eval_campaigns 属 tenant 数据面：organization_id + workspace_id 复合外键、
  FORCE RLS（org+ws GUC 过滤，与 0001 的 _WORKSPACE_TABLES 同策略）；RLS/授权
  模式逐行复制 0012；
- (organization_id, workspace_id, id) 唯一约束是 eval_runs.campaign_id 复合 FK 的
  引用目标（与 eval_runs → runs 的既有模式同构）；
- eval_runs 新增 5 个可空列：campaign_id（子运行关联，创建期冻结）+
  prereg/model/source/attempt_manifest_id（对 artifact_manifests 的复合 FK，
  创建期冻结，之后无 UPDATE 授权路径——S0 冻结契约「租户表无表级 UPDATE」不变）；
- campaign.status 只由全部子运行 sealed 推导，zhiwei_app 仅获 status/updated_at
  的列级 UPDATE（与 0012 的生命周期列授权同型）；DELETE 不授（CASCADE 随
  workspace 删除，应用层不物理删除）；
- downgrade 撤销全部授权、列、约束与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0013_evals_campaigns"
down_revision: str | None = "0012_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_CAMPAIGNS = "eval_campaigns"
_RUNS = "eval_runs"

_CAMPAIGN_UPDATE_COLUMNS = ("status", "updated_at")

_MANIFEST_FKS = (
    ("prereg_manifest_id", "fk_eval_runs_prereg_manifest"),
    ("model_manifest_id", "fk_eval_runs_model_manifest"),
    ("source_manifest_id", "fk_eval_runs_source_manifest"),
    ("attempt_manifest_id", "fk_eval_runs_attempt_manifest"),
)


def upgrade() -> None:
    op.create_table(
        _CAMPAIGNS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("suite_id", pg.UUID(), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("unit_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint("version > 0", name="version"),
        sa.CheckConstraint("unit_count >= 0", name="unit_count"),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.CheckConstraint(
            "status IN ('running', 'partial', 'completed')", name="campaign_status"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_campaigns_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_eval_campaigns_scope_id"),
    )

    op.execute(f'ALTER TABLE "{_CAMPAIGNS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_CAMPAIGNS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_CAMPAIGNS}_tenant_isolation" ON "{_CAMPAIGNS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CAMPAIGNS}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_CAMPAIGNS}" TO zhiwei_app')
    # 列级 UPDATE：status 由「全部子运行 sealed」推导的完成转移 + updated_at；
    # 划分内容列（suite_id/version/unit_count）不可变，表级 UPDATE 维持冻结契约不授。
    op.execute(
        f'GRANT UPDATE ({", ".join(_CAMPAIGN_UPDATE_COLUMNS)}) ON TABLE "{_CAMPAIGNS}" '
        "TO zhiwei_app"
    )

    # eval_runs 冻结引用列：campaign 关联 + 四个 artifact manifest 引用。
    # 引用目标分别是 eval_campaigns / artifact_manifests 的 (org, ws, id) 唯一约束，
    # 复合 FK 把引用钉在同一 tenant 内（与 eval_runs → runs 既有模式同构）。
    op.add_column(_RUNS, sa.Column("campaign_id", pg.UUID(), nullable=True))
    for column, _ in _MANIFEST_FKS:
        op.add_column(_RUNS, sa.Column(column, pg.UUID(), nullable=True))
    op.create_index("ix_eval_runs_campaign_id", _RUNS, ["campaign_id"])
    op.create_foreign_key(
        "fk_eval_runs_campaign",
        _RUNS,
        _CAMPAIGNS,
        ["organization_id", "workspace_id", "campaign_id"],
        ["organization_id", "workspace_id", "id"],
    )
    for column, fk_name in _MANIFEST_FKS:
        op.create_foreign_key(
            fk_name,
            _RUNS,
            "artifact_manifests",
            ["organization_id", "workspace_id", column],
            ["organization_id", "workspace_id", "id"],
        )


def downgrade() -> None:
    for _, fk_name in _MANIFEST_FKS:
        op.drop_constraint(fk_name, _RUNS, type_="foreignkey")
    op.drop_constraint("fk_eval_runs_campaign", _RUNS, type_="foreignkey")
    op.drop_index("ix_eval_runs_campaign_id", table_name=_RUNS)
    for column, _ in _MANIFEST_FKS:
        op.drop_column(_RUNS, column)
    op.drop_column(_RUNS, "campaign_id")
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CAMPAIGNS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_CAMPAIGNS}_tenant_isolation" ON "{_CAMPAIGNS}"')
    op.execute(f'ALTER TABLE "{_CAMPAIGNS}" DISABLE ROW LEVEL SECURITY')
    op.drop_table(_CAMPAIGNS)
