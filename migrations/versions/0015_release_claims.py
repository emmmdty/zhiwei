"""S9：Agent release 治理面与 Claim Registry——agent_releases + claim_registry（FORCE RLS）。

Revision ID: 0015_release_claims
Revises: 0014_cost_ledger

S9 plan Task 4：specs/s9 §5 的 release 生命周期与公开声明持久层。设计要点：

- 两表属 tenant 数据面：organization_id + workspace_id 复合外键、FORCE RLS
  （org+ws GUC 过滤，与 0013 同策略）；agent_releases 另有 (org, ws, agent_id) →
  agent_definitions 复合外键（release 钉在同一租户的 agent 上）；
- agent_releases 的 manifest payload/digest 冻结不可变：zhiwei_app 仅获
  state/rollout_policy/updated_at 的列级 UPDATE——manifest 内容 digest 覆盖全部
  依赖，改写即伪造发布身份；rollout_policy 独立存放是因为 rollback 只改 default
  pin（cohort 不重写）且 manifest 必须逐字节不变；
- claim_registry 的 statement/scope 冻结（口径是声明身份）；zhiwei_app 仅获
  status/evidence/bound_value/updated_at 列级 UPDATE（与 0012/0013 生命周期列
  授权同型）；DELETE 不授（CASCADE 随 workspace 删除）；
- (org, ws, claim_id) 唯一：claim_id 是租户内声明身份，重复注册 fail closed；
- downgrade 撤销全部授权、约束与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0015_release_claims"
down_revision: str | None = "0014_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_RELEASES = "agent_releases"
_CLAIMS = "claim_registry"

_RELEASE_UPDATE_COLUMNS = ("state", "rollout_policy", "updated_at")
_CLAIM_UPDATE_COLUMNS = ("status", "evidence", "bound_value", "updated_at")


def upgrade() -> None:
    op.create_table(
        _RELEASES,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("agent_id", pg.UUID(), nullable=False, index=True),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("manifest_digest", sa.String(length=71), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("rollout_policy", pg.JSONB(), nullable=False),
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
        sa.CheckConstraint("agent_version > 0", name="agent_version"),
        sa.CheckConstraint(
            "state IN ('draft', 'sandbox', 'evaluated', 'review', 'staged', "
            "'published', 'deprecated', 'retired')",
            name="release_state",
        ),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_agent_releases_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_id"],
            [
                "agent_definitions.organization_id",
                "agent_definitions.workspace_id",
                "agent_definitions.id",
            ],
            name="fk_agent_releases_agent",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_agent_releases_scope_id"),
    )
    op.create_table(
        _CLAIMS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("scope", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("bound_value", sa.Text(), nullable=True),
        sa.Column("evidence", pg.JSONB(), nullable=True),
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
            "status IN ('planned', 'implemented', 'offline_verified', "
            "'live_verified', 'retired')",
            name="claim_status",
        ),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_claim_registry_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "claim_id",
            name="uq_claim_registry_scope_claim",
        ),
    )

    op.execute(f'ALTER TABLE "{_RELEASES}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_RELEASES}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_RELEASES}_tenant_isolation" ON "{_RELEASES}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RELEASES}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_RELEASES}" TO zhiwei_app')
    # 列级 UPDATE：生命周期转移 + 活跃 rollout 策略（rollback 只改 default pin）；
    # manifest payload/digest 冻结不可变，表级 UPDATE 维持冻结契约不授。
    op.execute(
        f'GRANT UPDATE ({", ".join(_RELEASE_UPDATE_COLUMNS)}) ON TABLE "{_RELEASES}" '
        "TO zhiwei_app"
    )

    op.execute(f'ALTER TABLE "{_CLAIMS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_CLAIMS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_CLAIMS}_tenant_isolation" ON "{_CLAIMS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CLAIMS}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_CLAIMS}" TO zhiwei_app')
    # 列级 UPDATE：状态机/证据/绑定值 + updated_at；statement/scope 冻结不可变。
    op.execute(
        f'GRANT UPDATE ({", ".join(_CLAIM_UPDATE_COLUMNS)}) ON TABLE "{_CLAIMS}" '
        "TO zhiwei_app"
    )


def downgrade() -> None:
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_CLAIMS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_CLAIMS}_tenant_isolation" ON "{_CLAIMS}"')
    op.execute(f'ALTER TABLE "{_CLAIMS}" DISABLE ROW LEVEL SECURITY')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RELEASES}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_RELEASES}_tenant_isolation" ON "{_RELEASES}"')
    op.execute(f'ALTER TABLE "{_RELEASES}" DISABLE ROW LEVEL SECURITY')
    op.drop_table(_CLAIMS)
    op.drop_table(_RELEASES)
