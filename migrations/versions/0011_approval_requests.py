"""S2-T7：审批旅程持久化——approval_requests 表（FORCE RLS）。

Revision ID: 0011_approval_requests
Revises: 0010_eval_revision_grant

S2 spec §5 Web journey 要求「Approver 在独立账号批准/拒绝」——审批请求必须跨请求/
跨账号可见，域层（runtime/approvals.py）的内存管理器升级为 PG 存储。设计要点：

- 表属 tenant 数据面：organization_id + workspace_id 复合外键、FORCE RLS
  （org+ws GUC 过滤，与 0001 的 _WORKSPACE_TABLES 同策略）；
- zhiwei_app 获 SELECT/INSERT + **列级 UPDATE**（status/decided_by/
  decision_reason/decided_at——决策 CAS 只需这四列；表级 UPDATE 维持 S0
  冻结契约「租户表无表级 UPDATE」）；决策不可逆由终态触发器守护（纵深
  防御第二层）；
- request_id + run_id + task_id 绑定 workflow 的 RequestApproval 任务；
  exact input digest 列沿用域模型语义；
- downgrade 撤销全部授权与表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0011_approval_requests"
down_revision: str | None = "0010_eval_revision_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"

_TABLE = "approval_requests"

_TERMINAL_STATUSES = ("approved", "rejected", "revoked", "expired")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", pg.UUID(), nullable=False, primary_key=True),
        sa.Column("organization_id", pg.UUID(), nullable=False, index=True),
        sa.Column("workspace_id", pg.UUID(), nullable=False, index=True),
        sa.Column("run_id", pg.UUID(), nullable=False, index=True),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("input_digest", sa.String(length=71), nullable=False),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("last_input_modifier", sa.String(length=255), nullable=False),
        sa.Column("agent_identity", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", pg.UUID(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'revoked', 'expired')",
            name="approval_status",
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected')) = "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="approval_decision_fields",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requester",
            name="approval_sod_requester",
        ),
        sa.CheckConstraint("schema_version > 0", name="approval_schema_version"),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_approval_requests_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_approval_requests_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "run_id",
            "task_id",
            "input_digest",
            name="uq_approval_requests_task_digest",
        ),
    )
    op.create_index(
        "ix_approval_requests_run_status",
        _TABLE,
        ["run_id", "status"],
    )

    # 终态不可改写（纵深防御：应用层 CAS 之外的数据面守护）
    op.execute(
        """
        CREATE FUNCTION approval_requests_guard() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.status IN ('approved', 'rejected', 'revoked', 'expired')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'approval request % is terminal (%)', OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f'CREATE TRIGGER approval_requests_terminal_guard BEFORE UPDATE ON "{_TABLE}" '
        "FOR EACH ROW EXECUTE FUNCTION approval_requests_guard()"
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
    # 列级 UPDATE：决策 CAS 只改这四列；表级 UPDATE 维持冻结契约不授
    op.execute(
        f'GRANT UPDATE (status, decided_by, decision_reason, decided_at) '
        f'ON TABLE "{_TABLE}" TO zhiwei_app'
    )
    # 资源属主是 zhiwei_migrator（RLS 绕过用于种子/巡检），zhiwei_identity 不需要


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS approval_requests_terminal_guard ON approval_requests")
    op.execute("DROP FUNCTION IF EXISTS approval_requests_guard()")
    op.drop_index("ix_approval_requests_run_status", table_name=_TABLE)
    op.drop_table(_TABLE)
