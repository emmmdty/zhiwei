"""S9：Cost Ledger 持久化——cost_reservations + cost_reconciliations（FORCE RLS）。

Revision ID: 0014_cost_ledger
Revises: 0013_evals_campaigns

S9 plan Task 6：Cost Ledger 的 canonical 持久层（specs/s9 §6、ADR-002）。设计要点：

- cost 表属 tenant 数据面：organization_id + workspace_id 复合外键、FORCE RLS
  （org+ws GUC 过滤，与 0012 同策略）；reservations 另有 (org, ws, run_id) → runs
  复合外键（与 0011 approval_requests → runs 同构）；
- 两表只追加：zhiwei_app 仅获 SELECT/INSERT，无 UPDATE/DELETE——预订与对账都是
  一次性事实，纠正通过新的 reconcile variance 如实记录（ADR-002：ROI 指标不是
  门禁），不原地改写；
- variance 允许为负/超限（如实记录），amount_usd 非负、price_confidence 封闭枚举
  （exact/estimated）：无出处的金额不可审计，在数据面拒绝；
- 每预订至多一条 reconcile：唯一约束是域层 double-reconcile 拒绝的数据面备份；
- downgrade 撤销全部授权与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0014_cost_ledger"
down_revision: str | None = "0013_evals_campaigns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_RESERVATIONS = "cost_reservations"
_RECONCILIATIONS = "cost_reconciliations"


def upgrade() -> None:
    op.create_table(
        _RESERVATIONS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("run_id", pg.UUID(), nullable=False, index=True),
        sa.Column("amount_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("price_source", sa.String(length=255), nullable=False),
        sa.Column("price_confidence", sa.String(length=16), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount_usd >= 0", name="cost_reservation_amount"),
        sa.CheckConstraint(
            "price_confidence IN ('exact', 'estimated')",
            name="cost_reservation_price_confidence",
        ),
        sa.CheckConstraint("schema_version > 0", name="schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_cost_reservations_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_cost_reservations_run",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        _RECONCILIATIONS,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("reservation_id", pg.UUID(), nullable=False, index=True),
        sa.Column("reserved_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("actual_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("variance_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("retry_cost_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("child_run_cost_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("tool_external_cost_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
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
            name="fk_cost_reconciliations_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["cost_reservations.id"],
            name="fk_cost_reconciliations_reservation",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "reservation_id",
            name="uq_cost_reconciliations_reservation",
        ),
    )

    op.execute(f'ALTER TABLE "{_RESERVATIONS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_RESERVATIONS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_RESERVATIONS}_tenant_isolation" ON "{_RESERVATIONS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RESERVATIONS}" FROM PUBLIC')
    # 台账只追加：SELECT/INSERT 即完整能力，无 UPDATE/DELETE
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_RESERVATIONS}" TO zhiwei_app')

    op.execute(f'ALTER TABLE "{_RECONCILIATIONS}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{_RECONCILIATIONS}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{_RECONCILIATIONS}_tenant_isolation" ON "{_RECONCILIATIONS}" '
        f"USING (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}) "
        f"WITH CHECK (organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC})"
    )
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RECONCILIATIONS}" FROM PUBLIC')
    op.execute(f'GRANT SELECT, INSERT ON TABLE "{_RECONCILIATIONS}" TO zhiwei_app')


def downgrade() -> None:
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RECONCILIATIONS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_RECONCILIATIONS}_tenant_isolation" ON "{_RECONCILIATIONS}"')
    op.execute(f'ALTER TABLE "{_RECONCILIATIONS}" DISABLE ROW LEVEL SECURITY')
    op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{_RESERVATIONS}" FROM zhiwei_app')
    op.execute(f'DROP POLICY IF EXISTS "{_RESERVATIONS}_tenant_isolation" ON "{_RESERVATIONS}"')
    op.execute(f'ALTER TABLE "{_RESERVATIONS}" DISABLE ROW LEVEL SECURITY')
    op.drop_table(_RECONCILIATIONS)
    op.drop_table(_RESERVATIONS)
